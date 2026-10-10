"""Provider continuation changes preserve the exact unwritten input."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from test_cc_prepared_context import _register_live_provider
from tests.cc_runtime_fixtures import cc_target


def _claim(main_db):
    from infra.thread_driver import admit_input

    admission = admit_input(
        "main-1", role="user", content="the exact input", intent="apply_now",
        message_id="input-1", turn_id="turn-1", boot_epoch="test-boot",
        admitted_at="t", db=main_db,
    )
    assert admission.batch is not None
    return admission.batch


def _thread_row(main_db):
    return dict(main_db.execute(
        "SELECT * FROM threads WHERE id='main-1'",
    ).fetchone())


@pytest.mark.parametrize("model", ["claude-code/opus", "codex/gpt-5-codex"])
@pytest.mark.asyncio
async def test_claimed_input_refresh_uses_current_context_epoch(main_db, model):
    """Reusing the captured thread row makes every later refresh obsolete."""
    from infra.conductor import set_conductor
    from infra.input_render import prepare_claimed_batch
    from llm.resolution import resolve_execution_target

    main_db.execute("UPDATE threads SET model=? WHERE id='main-1'", (model,))
    main_db.commit()
    batch = _claim(main_db)
    target = resolve_execution_target(model, db=main_db)
    prepared = await prepare_claimed_batch(
        batch, thread_row=_thread_row(main_db), target=target,
    )
    assert prepared.prepared_context.runtime_context_epoch == 0
    assert set_conductor("workspace", "main-1", db=main_db)

    refreshed = await prepared.refresh_prepared_context()

    assert refreshed.runtime_context_epoch == 1
    assert refreshed.rendered.fit_input_identity == "turn-1"
    assert tuple(main_db.execute(
        "SELECT id,delivery_state,active_turn_id,write_started_at FROM messages "
        "WHERE id='input-1'",
    ).fetchone()) == ("input-1", "queued", "turn-1", None)


@pytest.mark.parametrize("invalidate", [False, True])
@pytest.mark.asyncio
async def test_cc_idle_resume_selects_current_input(main_db, invalidate):
    """A completed epoch change has no future owner to unblock an old handle."""
    from infra.conductor import set_conductor
    from infra.input_render import prepare_claimed_batch
    from subagents import cc_runtime

    provider_id = "provider-session-1"
    working_dir = _register_live_provider(main_db, provider_id)
    live = cc_runtime._Live(
        session=SimpleNamespace(
            working_dir=working_dir, thread_id=provider_id,
            resume_session_id=provider_id, reconstructed_transcript_path=None,
            returncode=None, pid=101,
        ),
        parent_thread_id=None, name="cc", model="opus",
        catalog_model_id=cc_target().catalog_id, runtime_context_epoch=0,
    )
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put("main-1", live)
    try:
        if invalidate:
            assert set_conductor("workspace", "main-1", db=main_db)
        batch = _claim(main_db)

        prepared = await prepare_claimed_batch(
            batch, thread_row=_thread_row(main_db), target=cc_target(),
        )

        assert prepared.prepared_context.mode == (
            "exact_replay" if invalidate else "provider_resume"
        )
        assert prepared.prepared_context.runtime_context_epoch == int(invalidate)
        assert prepared.prepared_context.rendered.fit_input_identity == "turn-1"
        assert cc_runtime._LIVE.get("main-1") is live
        assert tuple(main_db.execute(
            "SELECT id,delivery_state,write_started_at FROM messages WHERE id='input-1'",
        ).fetchone()) == ("input-1", "queued", None)
    finally:
        cc_runtime._LIVE.reset()


@pytest.mark.parametrize("invalidate", [False, True])
@pytest.mark.asyncio
async def test_codex_idle_resume_selects_current_input(main_db, monkeypatch, invalidate):
    """An idle app-server session must fit the same epoch as its destination."""
    from infra.conductor import set_conductor
    from infra.input_render import prepare_claimed_batch
    from llm.resolution import resolve_execution_target
    from registry.service import register_codex_thread
    from subagents.codex_destination import resolve_prepared_codex_context
    from subagents.codex_destination_render import codex_destination_scope
    from subagents.codex_runtime_spawn import _compaction_overrides
    from subagents.codex_runtime_state import _APP_SESSIONS, _CODEX_THREADS, _LIVE
    from subagents.harness_tool_bridge import build_tool_registry, dynamic_tool_definitions

    model = "codex/gpt-5-codex"
    main_db.execute("UPDATE threads SET model=? WHERE id='main-1'", (model,))
    main_db.commit()
    target = resolve_execution_target(model, db=main_db)
    batch = _claim(main_db)
    initial = await prepare_claimed_batch(
        batch, thread_row=_thread_row(main_db), target=target,
    )
    assert initial.prepared_context.mode == "exact_replay"
    _, working_dir = codex_destination_scope(
        "main-1", target_catalog_id=model, db=main_db,
    )
    registry = build_tool_registry(working_dir)
    session = SimpleNamespace(
        codex_thread_id="provider-thread-1", working_dir=working_dir,
        catalog_model_id=model, model=target.transport_model,
        expected_reported_model=target.expected_reported_model,
        runtime_context_epoch=0, returncode=None, tool_registry=registry,
        dynamic_tools=dynamic_tool_definitions(registry),
        config_overrides=_compaction_overrides(
            target.context_window,
            discovered_context_window=target.discovered_context_window,
        ),
    )
    register_codex_thread("main-1", session.codex_thread_id, db=main_db)
    monkeypatch.setitem(_APP_SESSIONS, "main-1", session)
    monkeypatch.setitem(_CODEX_THREADS, "main-1", session.codex_thread_id)
    if invalidate:
        assert set_conductor("workspace", "main-1", db=main_db)
    prepared = await prepare_claimed_batch(
        batch, thread_row=_thread_row(main_db), target=target,
    )

    assert prepared.prepared_context.mode == (
        "exact_replay" if invalidate else "provider_resume"
    )
    assert prepared.prepared_context.runtime_context_epoch == int(invalidate)
    assert prepared.prepared_context.rendered.fit_input_identity == "turn-1"
    assert _APP_SESSIONS["main-1"] is session
    assert tuple(main_db.execute(
        "SELECT id,delivery_state,write_started_at FROM messages WHERE id='input-1'",
    ).fetchone()) == ("input-1", "queued", None)

    async with _LIVE.lock("main-1"):
        final, _ = await resolve_prepared_codex_context(
            "main-1", target_catalog_id=model, current_user_text=initial.content,
            prepared_context=initial.prepared_context,
            refresh_prepared_context=initial.refresh_prepared_context,
            allow_cold_with_handle=True, refresh_admission_boundary=True,
        )
    assert final.mode == prepared.prepared_context.mode
    assert final.runtime_context_epoch == int(invalidate)
    assert final.rendered.fit_input_identity == "turn-1"


@pytest.mark.parametrize("timing", ["preflight", "post-fence", "pending-owner"])
@pytest.mark.asyncio
async def test_cc_execution_keeps_exact_input_after_repeated_context_changes(
    main_db, monkeypatch, fake_claude, timing,
):
    """Real admission/render/write/settlement, with only the CLI replaced."""
    from dataclasses import replace

    from infra.thread_driver import admit_input
    from subagents import cc_runtime
    from subagents.cc_stream import CCStreamSession

    # The disposable echo executable declares no real provider model identity.
    target = replace(cc_target(), expected_reported_model=None)
    original_feed = cc_runtime.feed_main
    original_write = CCStreamSession.feed
    final_epoch = []
    writes = []

    async def write_fake(session, text, **kwargs):
        writes.append(text)
        return await original_write(session, text, **kwargs)

    async def feed_fake(*args, **kwargs):
        assert kwargs["preadmitted_turn"] == batch.identity
        result = await original_feed(*args, binary=fake_claude, **kwargs)
        live = cc_runtime._LIVE.get("main-1")
        assert live is not None
        final_epoch.append(live.runtime_context_epoch)
        return result

    batch = _claim(main_db)
    monkeypatch.setattr(cc_runtime, "feed_main", feed_fake)
    monkeypatch.setattr(CCStreamSession, "feed", write_fake)
    result = await _execute_at_boundary(main_db, batch, target, timing, cc_runtime._LIVE)

    assert result.turn_id == batch.turn_id
    assert final_epoch == [3]
    assert len(writes) == 1 and "the exact input" in writes[0]
    assert tuple(main_db.execute(
        "SELECT id,delivery_state,write_outcome,active_turn_id,next_attempt_at "
        "FROM messages WHERE id='input-1'",
    ).fetchone()) == ("input-1", "delivered", "accepted", "turn-1", None)
    duplicate = admit_input(
        "main-1", role="user", content="the exact input", intent="apply_now",
        message_id="input-1", turn_id="duplicate-turn", boot_epoch="test-boot",
        admitted_at="later", db=main_db,
    )
    assert duplicate.batch is None
    assert len(writes) == 1
    assert main_db.execute(
        "SELECT COUNT(*) FROM messages WHERE id='input-1'",
    ).fetchone()[0] == 1


@pytest.mark.parametrize("timing", ["preflight", "post-fence", "pending-owner"])
@pytest.mark.asyncio
async def test_codex_execution_keeps_exact_input_after_repeated_context_changes(
    main_db, monkeypatch, timing,
):
    """Real Codex admission and write evidence with an inert app-server transport."""
    from codex_runtime_fixtures import _make_mock_app_session, _reset_runtime
    from infra.thread_driver import admit_input
    from llm.resolution import resolve_execution_target
    from subagents import codex_app_server, codex_runtime

    _reset_runtime()
    model = "codex/gpt-5-codex"
    main_db.execute("UPDATE threads SET model=? WHERE id='main-1'", (model,))
    main_db.commit()
    target = resolve_execution_target(model, db=main_db)
    batch = _claim(main_db)
    session = _make_mock_app_session("provider-thread-1")
    writes = []

    def session_factory(**kwargs):
        for name, value in kwargs.items():
            setattr(session, name, value)
        session.codex_thread_id = "provider-thread-1"
        session._closing = False
        return session

    async def start(**kwargs):
        live = codex_runtime._LIVE.get("main-1")
        assert live.corsia_turn == batch.identity
        assert live.runtime_context_epoch == 3
        kwargs["on_turn_write_observed"]()
        writes.append(session.task)
        kwargs["on_turn_drain_confirmed"]()
        return True

    original_feed = codex_runtime.feed_main

    async def feed_fake(*args, **kwargs):
        return await original_feed(*args, binary="inert-test-codex", **kwargs)

    monkeypatch.setattr(codex_runtime, "feed_main", feed_fake)
    monkeypatch.setattr(codex_app_server, "CodexAppServerSession", session_factory)
    session.start.side_effect = start
    try:
        result = await _execute_at_boundary(main_db, batch, target, timing, codex_runtime._LIVE)

        assert result.turn_id == batch.turn_id
        assert len(writes) == 1 and "the exact input" in writes[0]
        assert tuple(main_db.execute(
            "SELECT id,delivery_state,write_outcome,active_turn_id,next_attempt_at "
            "FROM messages WHERE id='input-1'",
        ).fetchone()) == ("input-1", "delivered", "accepted", "turn-1", None)
        duplicate = admit_input(
            "main-1", role="user", content="the exact input", intent="apply_now",
            message_id="input-1", turn_id="duplicate-turn", boot_epoch="test-boot",
            admitted_at="later", db=main_db,
        )
        assert duplicate.batch is None
        assert len(writes) == 1
    finally:
        _reset_runtime()


async def _execute_at_boundary(main_db, batch, target, timing, registry):
    """Exercise ordinary pending ownership across the timing variants."""
    import asyncio

    from infra.conductor import set_conductor
    from infra.thread_execution import execute_claimed_batch

    def change_context():
        for conductor in ("main-1", None, "main-1"):
            assert set_conductor("workspace", conductor, db=main_db)

    if timing == "preflight":
        change_context()
    fenced = asyncio.Event()

    async def after_fence(_target):
        if timing == "post-fence":
            change_context()
        fenced.set()

    async def execute():
        return await execute_claimed_batch(
            batch, thread_row=_thread_row(main_db), target=target,
            after_persist_before_dispatch=after_fence,
        )

    if timing != "pending-owner":
        return await asyncio.wait_for(execute(), timeout=10)
    task = None
    try:
        async with registry.lock("main-1"):
            task = asyncio.create_task(execute())
            await asyncio.wait_for(fenced.wait(), timeout=2)
            assert not task.done()
            assert tuple(main_db.execute(
                "SELECT delivery_state,write_outcome,active_turn_id FROM messages WHERE id='input-1'",
            ).fetchone()) == ("queued", None, "turn-1")
            change_context()
        return await asyncio.wait_for(task, timeout=10)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
