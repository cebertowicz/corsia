from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from cache_warm_fixtures import (
    cleanup_cache_warm_isolation,
    install_cache_warm_isolation,
    install_replay_candidates,
    install_resolved_omlx_session,
    install_warm_endpoint_session,
    make_checkpoint_tail_replay_selection,
    make_exact_replay_selection,
    _make_mock_agent,
)


@pytest.fixture(autouse=True)
def _cache_warm_isolation(monkeypatch):
    state = install_cache_warm_isolation(monkeypatch)
    install_replay_candidates(
        monkeypatch,
        exact=make_exact_replay_selection(
            "default-warm-replay",
            [[
                ("user", "earlier user msg"),
                ("assistant", "earlier assistant reply"),
            ]],
        ),
    )
    yield
    cleanup_cache_warm_isolation(state)


def test_thread_warm_private_naming_surface_preserves_route_identity() -> None:
    import importlib.util

    from fastapi.routing import APIRoute
    from fastapi.utils import generate_unique_id

    from api import thread_cache_warm
    from core import thread_warm

    assert hasattr(thread_warm, "cancel_thread_warm")
    assert hasattr(thread_warm, "await_thread_warm")
    assert hasattr(thread_warm, "start_thread_warm")
    assert hasattr(thread_cache_warm, "warm_session_cache")
    assert not hasattr(thread_cache_warm, "warm_thread_cache")
    assert importlib.util.find_spec("core.session_warm") is None
    route = next(
        route
        for route in thread_cache_warm.router.routes
        if isinstance(route, APIRoute) and route.path == "/threads/{thread_id}/warm"
    )
    assert route.name == "warm_session_cache"
    assert generate_unique_id(route) == (
        "warm_session_cache_threads__thread_id__warm_post"
    )


def test_warm_endpoint_probes_fitting_checkpoint_tail_when_exact_is_oversized(
    monkeypatch,
):
    from api import thread_cache_warm as api_runtime

    exact = make_exact_replay_selection(
        "sess-warm-endpoint-fit",
        [
            [("user", "x" * 120_000), ("assistant", "y" * 120_000)],
            [("user", "retained tail"), ("assistant", "retained answer")],
        ],
    )
    checkpoint_tail = make_checkpoint_tail_replay_selection(exact)
    install_replay_candidates(
        monkeypatch,
        exact=exact,
        checkpoint_tail=checkpoint_tail,
    )
    install_resolved_omlx_session(
        monkeypatch,
        api_runtime,
        "omlx@srv-1/model-1",
        context_window=60_000,
    )
    monkeypatch.setattr(
        "infra.db_async.run_in_db",
        AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
    )
    agent = _make_mock_agent(system_prompt="<SYS>")
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr("tools.catalog.advertised_tool_definitions", lambda _tools: [])
    provider = MagicMock()
    provider.probe_cache = AsyncMock(return_value={
        "ok": True,
        "model_loaded": True,
        "total_blocks": 1,
        "block_size": 1_000,
        "total_tokens": 1_000,
        "blocks_ssd_hot": 1,
        "blocks_ssd_disk": 0,
        "blocks_cold": 0,
    })
    monkeypatch.setattr("llm.providers.for_provider", MagicMock(return_value=provider))

    response = asyncio.run(
        api_runtime.warm_session_cache("sess-warm-endpoint-fit", token="t")
    )

    assert response["ok"] is True
    assert response["message"] == "Already warm"
    probed = provider.probe_cache.await_args.kwargs["messages"]
    assert "[CORSIA checkpoint]" in probed[0]["content"]


@pytest.mark.asyncio
async def test_native_warm_freezes_map_bytes_for_the_following_real_send(
    monkeypatch, main_db, tmp_path,
) -> None:
    from api import thread_cache_warm as api_runtime
    from core.context_replay import ReplayCandidates
    from core.dynamic_context_registry import collect_context_sources

    main_db.execute(
        "UPDATE threads SET model='oMLX/qwen',title='Current',agent_enabled=1,"
        "agent_responsibility='Current lane.' WHERE id='main-1'"
    )
    main_db.execute(
        "INSERT INTO threads "
        "(id,user_id,title,model,agent_enabled,agent_responsibility,created_at,updated_at) "
        "VALUES ('agent-2','steve','Other','oMLX/qwen',1,'Original lane.','t','t')"
    )
    main_db.execute(
        "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
        "VALUES ('first','main-1','user','hello','t',1)"
    )
    main_db.commit()
    selection = make_exact_replay_selection("main-1", [[("user", "hello")]])
    candidates = ReplayCandidates(
        exact=selection,
        checkpoint_tail=None,
        history_epoch=selection.history_epoch,
        sealed_boundary=selection.sealed_boundary,
        last_resort=selection,
    )
    target = SimpleNamespace(
        catalog_id="oMLX/qwen",
        transport_model="omlx@srv-1/qwen",
        context_window=32_768,
    )

    async def render(**kwargs):
        db = api_runtime.get_db()
        try:
            collection = collect_context_sources(
                thread_id=kwargs["thread_id"], project_id=None,
                workspace=tmp_path, db=db,
                selected_model=kwargs["selected_model"],
            )
        finally:
            db.close()
        source = next(
            row for row in collection.sources
            if row.source_id == "role:agent-owner-map"
        )
        return (
            [{"role": "system", "content": source.prompt_content}],
            {
                "history_messages": 1,
                "history_tokens": 1,
                "was_trimmed": False,
                "installed_context": collection.installed,
            },
        )

    monkeypatch.setattr(
        "core.native_context_prefix.build_real_send_prefix", render,
    )
    monkeypatch.setattr(api_runtime, "get_db", lambda: main_db)

    warm_messages, _ = await api_runtime._render_warm_replay(
        agent=object(),
        thread_id="main-1",
        project_id=None,
        target=target,
        candidates=candidates,
        eager_checkpoint_id=None,
        tool_defs=(),
    )
    main_db.execute(
        "UPDATE threads SET agent_responsibility='Changed lane.' WHERE id='agent-2'"
    )
    main_db.commit()
    send_messages, _ = await render(
        thread_id="main-1", selected_model="oMLX/qwen",
    )

    assert warm_messages == send_messages
    assert "Original lane." in warm_messages[0]["content"]
    assert "Changed lane." not in warm_messages[0]["content"]


def _admit_active_input(db, turn_id: str, content: str = "active"):
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input

    admission = admit_input(
        "main-1",
        role="user",
        content=content,
        intent="apply_now",
        message_id=f"input:{turn_id}",
        turn_id=turn_id,
        boot_epoch=BOOT_EPOCH,
        admitted_at="t-active",
        db=db,
    )
    assert admission.batch is not None
    return admission


def test_warm_endpoint_suppresses_active_turn_before_context_or_provider_work(
    monkeypatch, main_db,
):
    from api import thread_cache_warm as api_runtime

    install_resolved_omlx_session(
        monkeypatch, api_runtime, "omlx@srv-1/m1",
    )
    monkeypatch.setattr(api_runtime, "get_db", lambda: main_db)
    _admit_active_input(main_db, "active-warm-fence")
    get_agent = AsyncMock(side_effect=AssertionError("active turn built warm context"))
    monkeypatch.setattr("core.agent.get_agent", get_agent)

    response = asyncio.run(
        api_runtime.warm_session_cache("main-1", token="[REDACTED]")
    )

    assert response == {
        "ok": False,
        "skipped": True,
        "reason": "active_turn",
        "message": "Warm suppressed while the session has active delivery work",
    }
    get_agent.assert_not_awaited()


def test_warm_endpoint_rechecks_turn_ownership_after_prefix_build(
    monkeypatch, main_db,
):
    from api import thread_cache_warm as api_runtime
    from infra import state

    main_db.execute(
        "UPDATE models SET backend='network', litellm_model=?, context_window=? "
        "WHERE id='oMLX/qwen'",
        ("omlx@srv-1/qwen", 32_768),
    )
    main_db.execute(
        "UPDATE model_profiles SET context_window=? WHERE model_id='oMLX/qwen'",
        (32_768,),
    )
    main_db.commit()
    install_warm_endpoint_session(
        monkeypatch,
        api_runtime,
        "oMLX/qwen",
        db=main_db,
    )
    agent = _make_mock_agent(system_prompt="SYSTEM")
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=agent))

    async def build_prefix(**_kwargs):
        _admit_active_input(main_db, "late-active-warm-fence")
        return (
            [{"role": "system", "content": "SYSTEM"}],
            {"history_messages": 0, "history_tokens": 0, "was_trimmed": False},
        )

    monkeypatch.setattr("core.native_context_prefix.build_real_send_prefix", build_prefix)
    provider = MagicMock()
    provider.probe_cache = AsyncMock(return_value={"ok": False})
    provider.warm_cache = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("llm.providers.for_provider", MagicMock(return_value=provider))

    response = asyncio.run(
        api_runtime.warm_session_cache("main-1", token="[REDACTED]")
    )

    assert response == {
        "ok": False,
        "skipped": True,
        "reason": "active_turn",
        "message": "Warm suppressed while the session has active delivery work",
    }
    assert "main-1" not in state._active_warm_tasks
    provider.warm_cache.assert_not_awaited()


def test_warm_endpoint_suppresses_queued_input_before_context_or_provider_work(
    monkeypatch, main_db,
):
    from api import thread_cache_warm as api_runtime
    from infra.input_admission import accept_thread_input

    install_resolved_omlx_session(
        monkeypatch, api_runtime, "omlx@srv-1/m1",
    )
    monkeypatch.setattr(api_runtime, "get_db", lambda: main_db)
    accept_thread_input(
        "main-1",
        role="user",
        content="production-owned runnable head",
        intent="apply_now",
        message_id="queued-warm-fence",
        db=main_db,
    )
    get_agent = AsyncMock(side_effect=AssertionError("runnable head built warm context"))
    monkeypatch.setattr("core.agent.get_agent", get_agent)

    response = asyncio.run(
        api_runtime.warm_session_cache("main-1", token="[REDACTED]")
    )

    assert response == {
        "ok": False,
        "skipped": True,
        "reason": "queued_input",
        "message": "Warm suppressed while the session has active delivery work",
    }
    get_agent.assert_not_awaited()


@pytest.mark.parametrize(
    ("harness", "provider", "backend", "enabled", "available"),
    [
        ("native", "omlx", "network", False, True),
        ("native", "omlx", "network", True, False),
        ("codex", "codex", "codex", True, True),
        ("native", "external", "network", True, True),
    ],
    ids=("disabled", "unavailable", "non-native", "non-omlx"),
)
def test_warm_endpoint_rejects_ineligible_catalog_target_before_context_build(
    monkeypatch, main_db, harness, provider, backend, enabled, available,
):
    from api import thread_cache_warm as api_runtime

    # A misleading oMLX-shaped transport proves typed catalog fields, not the
    # string prefix, decide eligibility.
    main_db.execute(
        "UPDATE models SET harness=?, provider=?, backend=?, enabled=?, "
        "available=?, litellm_model=? WHERE id='oMLX/qwen'",
        (
            harness, provider, backend, int(enabled), int(available),
            "omlx@srv-shape-is-not-authority/qwen",
        ),
    )
    main_db.commit()
    install_warm_endpoint_session(
        monkeypatch,
        api_runtime,
        "oMLX/qwen",
        db=main_db,
    )
    get_agent = AsyncMock(side_effect=AssertionError("ineligible target built context"))
    monkeypatch.setattr("core.agent.get_agent", get_agent)

    response = asyncio.run(api_runtime.warm_session_cache("sess-1", token="t"))

    assert response["ok"] is False
    assert "warmable" in response["message"].lower()
    get_agent.assert_not_awaited()


@pytest.mark.parametrize("missing_context", [False, True], ids=("known", "refreshed"))
def test_warm_endpoint_uses_final_target_for_prefix_probe_and_dispatch(
    monkeypatch, main_db, missing_context,
):
    from api import thread_cache_warm as api_runtime
    from infra import state
    from llm.resolution import resolve_thread_target as real_resolve

    catalog_id = "oMLX/qwen"
    stale_selection = "omlx@stale-server/qwen"
    stale_transport = "omlx@srv-stale/qwen"
    final_transport = "omlx@fresh-server/qwen"
    # Below DEFAULT_CHECKPOINT_WINDOW, so the refreshed row's own window is the
    # one in force whether or not a profile saves it.
    final_window = 200_321
    initial_transport = stale_transport if missing_context else final_transport
    initial_window = None if missing_context else final_window
    main_db.execute(
        "UPDATE models SET backend='network', litellm_model=?, context_window=? "
        "WHERE id=?",
        (initial_transport, initial_window, catalog_id),
    )
    main_db.execute(
        "UPDATE model_profiles SET context_window=? WHERE model_id=?",
        (initial_window, catalog_id),
    )
    main_db.commit()
    selected_model = catalog_id if missing_context else stale_selection
    install_warm_endpoint_session(
        monkeypatch,
        api_runtime,
        selected_model,
        db=main_db,
        project_id="proj-1",
    )
    resolved: list[tuple[str, object]] = []

    def _resolve(selected, *, thread_id, db):
        resolved.append((selected, db))
        return real_resolve(selected, thread_id=thread_id, db=db)

    monkeypatch.setattr(api_runtime, "resolve_thread_target", _resolve, raising=False)

    async def _repair(model: str):
        assert model == catalog_id
        main_db.execute(
            "UPDATE models SET litellm_model=?, context_window=? WHERE id=?",
            (final_transport, final_window, catalog_id),
        )
        main_db.commit()

    repair_context = AsyncMock(side_effect=_repair)
    monkeypatch.setattr(
        api_runtime,
        "ensure_context_window",
        repair_context,
        raising=False,
    )
    agent = _make_mock_agent(system_prompt="SYSTEM")
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=agent))
    build_prefix = AsyncMock(return_value=(
        [{"role": "system", "content": "SYSTEM"}],
        {"history_messages": 0, "history_tokens": 0, "was_trimmed": False},
    ))
    monkeypatch.setattr("core.native_context_prefix.build_real_send_prefix", build_prefix)
    provider = MagicMock()
    provider.probe_cache = AsyncMock(return_value={"ok": False})
    provider.warm_cache = AsyncMock(return_value={"ok": True})
    provider_factory = MagicMock(return_value=provider)
    monkeypatch.setattr("llm.providers.for_provider", provider_factory)

    async def _run():
        response = await api_runtime.warm_session_cache("sess-1", token="t")
        pending = state._active_warm_tasks.get("sess-1")
        if pending is not None:
            await asyncio.wait_for(asyncio.shield(pending), timeout=2)
        return response

    response = asyncio.run(_run())

    assert response["ok"] is True
    expected_resolutions = 2 if missing_context else 1
    assert resolved == [(selected_model, main_db)] * expected_resolutions
    if missing_context:
        repair_context.assert_awaited_once_with(catalog_id)
    else:
        repair_context.assert_not_awaited()
    build_prefix.assert_awaited_once_with(
        agent=agent,
        thread_id="sess-1",
        turn_model=final_transport,
        fresh_context=False,
        project_id="proj-1",
        model_context_window=final_window,
        replay_selection=ANY,
        tool_definitions=(),
        selected_model=catalog_id,
    )
    provider_factory.assert_called_once_with("omlx")
    for operation in (provider.probe_cache, provider.warm_cache):
        operation.assert_awaited_once_with(
            {"server_id": "fresh-server"},
            model_id="qwen",
            messages=[{"role": "system", "content": "SYSTEM"}],
            tools=[],
        )


@pytest.mark.parametrize("discovery_fails", [False, True], ids=("unknown", "failed"))
def test_warm_endpoint_rejects_unknown_context_after_discovery_before_build_or_probe(
    monkeypatch, main_db, discovery_fails,
):
    from api import thread_cache_warm as api_runtime

    catalog_id = "oMLX/qwen"
    main_db.execute(
        "UPDATE models SET backend='network', litellm_model=?, context_window=0 "
        "WHERE id=?",
        ("omlx@srv-unknown/qwen", catalog_id),
    )
    main_db.execute(
        "UPDATE model_profiles SET context_window=0 WHERE model_id=?",
        (catalog_id,),
    )
    main_db.commit()
    install_warm_endpoint_session(
        monkeypatch,
        api_runtime,
        catalog_id,
        db=main_db,
    )
    discover = None
    if discovery_fails:
        discover = AsyncMock(side_effect=RuntimeError("discovery unavailable"))
        monkeypatch.setattr(
            "llm.catalog_network_models.discover_network_models", discover
        )
        repair_context = AsyncMock(
            wraps=api_runtime.ensure_context_window
        )
    else:
        repair_context = AsyncMock()
    monkeypatch.setattr(
        api_runtime,
        "ensure_context_window",
        repair_context,
        raising=False,
    )
    get_agent = AsyncMock(side_effect=AssertionError("unknown target built context"))
    monkeypatch.setattr("core.agent.get_agent", get_agent)
    provider_factory = MagicMock(
        side_effect=AssertionError("unknown target probed provider")
    )
    monkeypatch.setattr("llm.providers.for_provider", provider_factory)

    response = asyncio.run(api_runtime.warm_session_cache("sess-1", token="t"))

    assert response == {
        "ok": False,
        "message": f"Exact context-window evidence is unknown for {catalog_id}",
    }
    repair_context.assert_awaited_once_with(catalog_id)
    if discover is not None:
        discover.assert_awaited_once_with("srv-unknown")
    get_agent.assert_not_awaited()
    provider_factory.assert_not_called()


def test_warm_endpoint_does_not_fall_back_to_volatile_thread_model(
    monkeypatch, main_db,
):
    from api import thread_cache_warm as api_runtime
    from infra import state

    install_warm_endpoint_session(
        monkeypatch,
        api_runtime,
        "",
        db=main_db,
    )
    state._active_thread_models["sess-1"] = {
        "model_id": "omlx@srv-volatile/model-1",
    }
    get_agent = AsyncMock(side_effect=AssertionError("volatile fallback built context"))
    monkeypatch.setattr("core.agent.get_agent", get_agent)
    try:
        response = asyncio.run(api_runtime.warm_session_cache("sess-1", token="t"))
    finally:
        state._active_thread_models.pop("sess-1", None)

    assert response["ok"] is False
    assert "no model configured" in response["message"].lower()
    get_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_warm_endpoint_passes_explicit_model_id_to_provider(monkeypatch):
    from api import thread_cache_warm as api_runtime

    install_resolved_omlx_session(
        monkeypatch,
        api_runtime,
        "omlx@srv-1/Local-Model-A3B-8bit",
        context_window=32768,
    )
    monkeypatch.setattr("llm.capabilities.get_model_context_window", lambda _model: 32768)

    mock_agent = _make_mock_agent(system_prompt="SYSTEM")
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=mock_agent))
    monkeypatch.setattr(
        "infra.db_async.run_in_db",
        AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
    )

    captured = {}
    async def _fake_warm(cfg, *, model_id, messages, tools=None):
        captured["cfg"] = cfg
        captured["model_id"] = model_id
        captured["messages"] = messages
        captured["tools"] = tools
        return {"ok": True}

    fake_provider = MagicMock()
    fake_provider.warm_cache = _fake_warm
    fake_provider.probe_cache = AsyncMock(return_value={"ok": False, "message": "stub"})

    monkeypatch.setattr("llm.providers.for_provider", lambda _name: fake_provider)

    resp = await api_runtime.warm_session_cache("sess-1", token="t")
    from infra import state
    pending = state._active_warm_tasks.get("sess-1")
    if pending is not None:
        await asyncio.wait_for(asyncio.shield(pending), timeout=2)

    assert resp["ok"] is True
    assert captured["model_id"] == "Local-Model-A3B-8bit"
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][0]["content"] == "SYSTEM"
    assert len(captured["messages"]) == 3
    assert captured["messages"][1]["role"] == "user"
    assert captured["messages"][2]["role"] == "assistant"


@pytest.mark.parametrize(
    ("effort", "expected_shape"),
    [
        ("auto", {}),
        ("none", {"chat_template_kwargs": {"enable_thinking": False}}),
        # The level travels as reasoning_effort, and warm must render with it:
        # the chat template injects it, so omitting it would warm a prefix the
        # turn never sends.
        ("low", {"reasoning_effort": "low"}),
    ],
)
def test_warm_endpoint_uses_normal_turn_omlx_prompt_shape(
    monkeypatch,
    effort,
    expected_shape,
):
    from api import thread_cache_warm as api_runtime
    from infra import state

    install_resolved_omlx_session(
        monkeypatch,
        api_runtime,
        "omlx@srv-1/model-1",
        reasoning_effort=effort,
    )
    monkeypatch.setattr(
        "core.agent.get_agent",
        AsyncMock(return_value=_make_mock_agent(system_prompt="SYSTEM")),
    )
    monkeypatch.setattr(
        "core.native_context_prefix.build_real_send_prefix",
        AsyncMock(return_value=(
            [{"role": "system", "content": "SYSTEM"}],
            {"history_messages": 0, "history_tokens": 0, "was_trimmed": False},
        )),
    )
    provider = MagicMock()
    provider.probe_cache = AsyncMock(return_value={"ok": False})
    provider.warm_cache = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("llm.providers.for_provider", lambda _name: provider)

    async def drive():
        result = await api_runtime.warm_session_cache("sess-1", token="t")
        task = state._active_warm_tasks.get("sess-1")
        if task is not None:
            await task
        return result

    assert asyncio.run(drive())["ok"] is True
    for operation in (provider.probe_cache, provider.warm_cache):
        kwargs = operation.await_args.kwargs
        assert {
            key: kwargs[key]
            for key in ("chat_template_kwargs", "thinking_budget", "reasoning_effort")
            if key in kwargs
        } == expected_shape


def test_warm_endpoint_returns_loud_fail_for_unloaded_target(monkeypatch):
    from api import thread_cache_warm as api_runtime

    install_resolved_omlx_session(
        monkeypatch, api_runtime, "omlx@srv-1/unloaded-model",
    )

    mock_agent = _make_mock_agent(system_prompt="S")
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=mock_agent))
    monkeypatch.setattr(
        "infra.db_async.run_in_db",
        AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
    )
    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda m: 128000,
    )

    warm_called = False
    async def _spy_warm(*_a, **_kw):
        nonlocal warm_called
        warm_called = True
        return {"ok": True}

    fake_provider = MagicMock()
    fake_provider.warm_cache = _spy_warm
    fake_provider.probe_cache = AsyncMock(return_value={
        "ok": True,
        "model_id": "unloaded-model",
        "model_loaded": False,
        "total_tokens": 0,
        "block_size": 0,
        "total_blocks": 0,
        "blocks_ssd_hot": 0,
        "blocks_ssd_disk": 0,
        "blocks_cold": 0,
    })
    monkeypatch.setattr("llm.providers.for_provider", lambda _name: fake_provider)

    resp = asyncio.run(api_runtime.warm_session_cache("sess-1", token="t"))
    assert resp["ok"] is False
    assert "model not loaded" in resp["message"]
    assert "unloaded-model" in resp["message"]
    assert not warm_called


@pytest.mark.asyncio
async def test_cold_probe_falls_back_to_warm_and_reports_starting_progress(monkeypatch):
    from api import thread_cache_warm as api_runtime
    from infra import state

    install_resolved_omlx_session(monkeypatch, api_runtime, "omlx@srv-1/model-1")
    monkeypatch.setattr(
        "core.agent.get_agent", AsyncMock(return_value=_make_mock_agent(system_prompt="SYSTEM")),
    )
    provider = MagicMock()
    provider.probe_cache = AsyncMock(return_value={
        "ok": True,
        "model_loaded": True,
        "total_blocks": 8,
        "block_size": 2_048,
        "total_tokens": 16_384,
        "blocks_ssd_hot": 0,
        "blocks_ssd_disk": 0,
        "blocks_cold": 8,
    })
    provider.warm_cache = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("llm.providers.for_provider", lambda _name: provider)

    response = await api_runtime.warm_session_cache("sess-1", token="[REDACTED]")
    await state._active_warm_tasks["sess-1"]

    assert response == {
        "ok": True,
        "message": "Warm request submitted",
        "progress": {
            "total_blocks": 8,
            "cold_blocks_at_start": 8,
            "block_size": 2_048,
            "total_tokens": 16_384,
            "ready_blocks": 0,
        },
    }
    provider.warm_cache.assert_awaited_once()


def test_probe_memory_hot_allows_only_the_uncacheable_partial_tail():
    from api.thread_cache_warm import _probe_is_memory_hot

    assert _probe_is_memory_hot({
        "ok": True,
        "total_tokens": 16_253,
        "block_size": 2_048,
        "total_blocks": 8,
        "blocks_ssd_hot": 7,
        "blocks_ssd_disk": 0,
        "blocks_cold": 1,
    })


def test_probe_memory_hot_requires_every_complete_block():
    from api.thread_cache_warm import _probe_is_memory_hot

    assert not _probe_is_memory_hot({
        "ok": True,
        "total_tokens": 16_384,
        "block_size": 2_048,
        "total_blocks": 8,
        "blocks_ssd_hot": 7,
        "blocks_ssd_disk": 0,
        "blocks_cold": 1,
    })


def test_warm_endpoint_deduplicates_same_session_model_prefix(monkeypatch):
    from api import thread_cache_warm as api_runtime
    from infra import state

    install_resolved_omlx_session(
        monkeypatch, api_runtime, "omlx@srv-1/m1",
    )
    mock_agent = _make_mock_agent(system_prompt="S")
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=mock_agent))
    build_prefix = AsyncMock(return_value=(
        [{"role": "system", "content": "same-prefix"}],
        {"history_messages": 0, "history_tokens": 0, "was_trimmed": False},
    ))
    monkeypatch.setattr("core.native_context_prefix.build_real_send_prefix", build_prefix)
    started = asyncio.Event()
    release = asyncio.Event()
    warm_calls = 0

    async def _warm(cfg, *, model_id, messages, tools=None):
        nonlocal warm_calls
        warm_calls += 1
        started.set()
        await release.wait()
        return {"ok": True}

    fake_provider = MagicMock()
    fake_provider.probe_cache = AsyncMock(return_value={"ok": False})
    fake_provider.warm_cache = _warm
    monkeypatch.setattr("llm.providers.for_provider", lambda _name: fake_provider)

    async def _drive():
        await api_runtime.warm_session_cache("sess-1", token="")
        first_task = state._active_warm_tasks["sess-1"]
        await asyncio.wait_for(started.wait(), timeout=1)
        second = await api_runtime.warm_session_cache("sess-1", token="")

        assert state._active_warm_tasks["sess-1"] is first_task
        assert second == {
            "ok": True,
            "skipped": True,
            "message": "Identical warm already in progress",
        }
        assert warm_calls == 1
        release.set()
        await first_task

    asyncio.run(_drive())


@pytest.mark.asyncio
async def test_omlx_warm_key_probe_and_payload_match_real_send_sanitization(
    monkeypatch,
) -> None:
    from api import thread_cache_warm as api_runtime
    from core.thread_warm import warm_prefix_key
    from infra import state
    from llm import base
    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{}"},
    }
    prefix = [
        {"role": "system", "content": "", "corsia_only": "drop"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool_call],
            "corsia_only": "drop",
        },
        {
            "role": "assistant",
            "reasoning_content": "retained reasoning",
            "corsia_only": "drop",
        },
    ]
    expected_prefix = [
        {"role": "system", "content": "(empty)"},
        {"role": "assistant", "content": None, "tool_calls": [tool_call]},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "retained reasoning",
        },
    ]
    sent: dict = {}
    async def capture_send(kwargs, **_kwargs):
        sent["messages"] = kwargs["messages"]
        return object()
    monkeypatch.setattr(base, "_completion_with_retries", capture_send)
    await base.base_completion(
        [*prefix, {"role": "user", "content": "tail", "corsia_only": "drop"}],
        model="omlx@srv-1/model-1",
    )
    provider = MagicMock()
    provider.probe_cache = AsyncMock(return_value={"ok": False})
    provider.warm_cache = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("llm.providers.for_provider", lambda _name: provider)
    install_resolved_omlx_session(
        monkeypatch,
        api_runtime,
        "omlx@srv-1/model-1",
    )
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    agent = _make_mock_agent()
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(
        "core.native_context_prefix.build_real_send_prefix",
        AsyncMock(return_value=(
            prefix,
            {"history_messages": 2, "history_tokens": 20, "was_trimmed": False},
        )),
    )
    monkeypatch.setattr("tools.catalog.advertised_tool_definitions", lambda _tools: tools)

    result = await api_runtime.warm_session_cache("thread-native", token="t")
    task = state._active_warm_tasks.get("thread-native")
    assert result["ok"] is True
    assert task is not None
    assert sent["messages"][:-1] == expected_prefix
    assert getattr(task, "_corsia_warm_key") == warm_prefix_key(
        "thread-native", "model-1", expected_prefix, tools,
    )
    assert await task == {"ok": True}
    provider.probe_cache.assert_awaited_once_with(
        {"server_id": "srv-1"},
        model_id="model-1",
        messages=expected_prefix,
        tools=tools,
    )
    provider.warm_cache.assert_awaited_once_with(
        {"server_id": "srv-1"},
        model_id="model-1",
        messages=expected_prefix,
        tools=tools,
    )


def test_warm_endpoint_cancels_and_joins_changed_prefix_before_replacement(monkeypatch):
    from api import thread_cache_warm as api_runtime
    from infra import state

    install_resolved_omlx_session(
        monkeypatch, api_runtime, "omlx@srv-1/m1",
    )

    mock_agent = _make_mock_agent(system_prompt="S")
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=mock_agent))
    build_prefix = AsyncMock(side_effect=[
        (
            [{"role": "system", "content": "prefix-one"}],
            {"history_messages": 0, "history_tokens": 0, "was_trimmed": False},
        ),
        (
            [{"role": "system", "content": "prefix-two"}],
            {"history_messages": 0, "history_tokens": 0, "was_trimmed": False},
        ),
    ])
    monkeypatch.setattr("core.native_context_prefix.build_real_send_prefix", build_prefix)

    warm_started = asyncio.Event()
    warm_cancelled = asyncio.Event()
    warm_joined = asyncio.Event()

    async def _slow_warm(cfg, *, model_id, messages, tools=None):
        warm_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            warm_cancelled.set()
            await asyncio.sleep(0)
            warm_joined.set()
            raise
        return {"ok": True}

    async def _fast_warm(cfg, *, model_id, messages, tools=None):
        assert warm_joined.is_set(), "replacement started before prior warm joined"
        return {"ok": True}

    fake_provider = MagicMock()
    fake_provider.probe_cache = AsyncMock(return_value={"ok": False})

    async def _drive():
        fake_provider.warm_cache = _slow_warm
        monkeypatch.setattr("llm.providers.for_provider", lambda _n: fake_provider)
        await api_runtime.warm_session_cache("sess-1", token="t")
        first_task = state._active_warm_tasks.get("sess-1")
        assert first_task is not None

        await asyncio.wait_for(warm_started.wait(), timeout=2)

        fake_provider.warm_cache = _fast_warm
        await api_runtime.warm_session_cache("sess-1", token="t")
        assert warm_cancelled.is_set(), "Prior warm task was not cancelled"
        assert warm_joined.is_set(), "Prior warm task was not joined"
        assert first_task.cancelled() or first_task.done()

    asyncio.run(_drive())

def test_changed_prefix_rechecks_turn_admission_after_join(monkeypatch, main_db):
    from api import thread_cache_warm as api_runtime
    from infra import state

    main_db.execute(
        "UPDATE models SET backend='network', litellm_model=?, context_window=? "
        "WHERE id='oMLX/qwen'",
        ("omlx@srv-1/qwen", 32_768),
    )
    main_db.execute(
        "UPDATE model_profiles SET context_window=? WHERE model_id='oMLX/qwen'",
        (32_768,),
    )
    main_db.execute(
        "UPDATE threads SET model='oMLX/qwen' WHERE id='main-1'",
    )
    main_db.commit()
    install_warm_endpoint_session(
        monkeypatch,
        api_runtime,
        "oMLX/qwen",
        db=main_db,
    )
    agent = _make_mock_agent(system_prompt="SYSTEM")
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=agent))
    build_prefix = AsyncMock(side_effect=[
        (
            [{"role": "system", "content": "prefix-one"}],
            {"history_messages": 0, "history_tokens": 0, "was_trimmed": False},
        ),
        (
            [{"role": "system", "content": "prefix-two"}],
            {"history_messages": 0, "history_tokens": 0, "was_trimmed": False},
        ),
    ])
    monkeypatch.setattr("core.native_context_prefix.build_real_send_prefix", build_prefix)

    first_started = asyncio.Event()
    replacement_started = asyncio.Event()
    warm_calls = 0

    async def warm_cache(_cfg, *, model_id, messages, tools=None):
        nonlocal warm_calls
        warm_calls += 1
        if warm_calls > 1:
            replacement_started.set()
            return {"ok": True}
        first_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            _admit_active_input(
                main_db, "admitted-during-warm-join", "queued successor",
            )
            await asyncio.sleep(0)
            raise

    provider = MagicMock()
    provider.probe_cache = AsyncMock(return_value={"ok": False})
    provider.warm_cache = warm_cache
    monkeypatch.setattr("llm.providers.for_provider", MagicMock(return_value=provider))

    async def _drive():
        first = await api_runtime.warm_session_cache("main-1", token="[REDACTED]")
        assert first["ok"] is True
        await first_started.wait()
        second = await api_runtime.warm_session_cache("main-1", token="[REDACTED]")
        await asyncio.sleep(0)
        return second

    response = asyncio.run(_drive())

    assert response == {
        "ok": False,
        "skipped": True,
        "reason": "active_turn",
        "message": "Warm suppressed while the session has active delivery work",
    }
    assert not replacement_started.is_set()
    assert warm_calls == 1
    assert "main-1" not in state._active_warm_tasks


def test_concurrent_changed_prefix_replacements_never_overlap_existing_warm():
    from core.thread_warm import start_thread_warm
    from infra import state

    async def _drive():
        old_started = asyncio.Event()
        old_cancelled = asyncio.Event()
        allow_old_join = asyncio.Event()
        old_joined = asyncio.Event()
        replacement_started = asyncio.Event()
        replacement_release = asyncio.Event()

        async def old_warm():
            old_started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                old_cancelled.set()
                await allow_old_join.wait()
                old_joined.set()
                raise

        async def replacement_warm():
            replacement_started.set()
            await replacement_release.wait()
            return {"ok": True}

        old_task, _, _ = await start_thread_warm(
            "sess-race",
            "prefix-0",
            old_warm,
            model="omlx@srv-1/model-1", admission_check=lambda: None,
        )
        await old_started.wait()
        first_replacement = asyncio.create_task(
            start_thread_warm(
                "sess-race",
                "prefix-1",
                replacement_warm,
                model="omlx@srv-1/model-1", admission_check=lambda: None,
            )
        )
        await old_cancelled.wait()
        second_replacement = asyncio.create_task(
            start_thread_warm(
                "sess-race",
                "prefix-2",
                replacement_warm,
                model="omlx@srv-1/model-1", admission_check=lambda: None,
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        overlapped_unjoined_owner = (
            replacement_started.is_set() and not old_joined.is_set()
        )

        allow_old_join.set()
        first_result, second_result = await asyncio.gather(
            first_replacement,
            second_replacement,
        )
        assert old_task.done()
        assert not overlapped_unjoined_owner
        assert state._active_warm_tasks["sess-race"] in {
            first_result[0],
            second_result[0],
        }

        replacement_release.set()
        for task in {first_result[0], second_result[0]}:
            if not task.done():
                task.cancel()
        await asyncio.gather(
            first_result[0],
            second_result[0],
            return_exceptions=True,
        )

    asyncio.run(_drive())


def test_model_load_does_not_warm_without_composer_intent(monkeypatch, mock_posthog):
    from api import network_servers as ns

    monkeypatch.setattr(ns.nd, "get_server", lambda sid: {"id": sid, "provider": "omlx"})
    monkeypatch.setattr(ns, "notify_models_changed", lambda: None)
    monkeypatch.setattr(ns.nd, "_LOADED_BY_SERVER", {})
    monkeypatch.setattr(ns.nd, "discover_and_notify", AsyncMock(return_value=None))

    warm_invocations = []

    async def _spy_warm(cfg, *, model_id, messages, tools=None):
        warm_invocations.append({"model_id": model_id, "cfg": cfg})
        return {"ok": True}

    fake_provider = MagicMock()
    fake_provider.load_model = AsyncMock(return_value={"ok": True})
    fake_provider.warm_cache = _spy_warm
    monkeypatch.setattr(ns, "for_provider", lambda _name: fake_provider)

    mock_agent = MagicMock()
    mock_agent.tools.get_definitions = MagicMock(return_value=[])

    async def _drive():
        with patch("core.agent.get_agent", new=AsyncMock(return_value=mock_agent)), \
             patch("core.context_builder.ContextBuilder",
                   lambda *_a, **_kw: MagicMock(build_system_prompt=lambda **_: "S")):
            body = ns.ModelBody(model="qwen3.6-35b-a3b-mxfp8")
            await ns.load_model("srv-1", body)
            await asyncio.sleep(0)

    asyncio.run(_drive())

    assert warm_invocations == []

def test_execute_turn_leaves_warm_owner_for_native_preflight(monkeypatch):
    from infra import state
    from ws import turn as turn_mod

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _slow_warm():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def _drive():
        task = asyncio.create_task(_slow_warm())
        state._active_warm_tasks["sess-x"] = task
        await asyncio.wait_for(started.wait(), timeout=1)

        class FakeWs:
            def __init__(self):
                self.sent: list[dict] = []

            async def send_json(self, payload):
                self.sent.append(payload)

        async def fake_setup_turn(*_args, **_kwargs):
            from core.turn_core import TurnIdentity

            return (
                object(),
                "omlx@test/model",
                {},
                asyncio.Event(),
                TurnIdentity("turn-1", 1),
                (ws.send_json, [None]),
                object(),
                "hello",
                [],
                lambda _identity: None,
                None,
            )

        async def fake_run_tracked_turn(_turn):
            assert state._active_warm_tasks["sess-x"] is task
            assert not task.done()
            return None

        async def fake_post_turn_tasks(**_kwargs):
            return None

        ws = FakeWs()
        from llm.resolution import ExecutionTarget
        target = ExecutionTarget(
            catalog_id="omlx@test/model", harness="native", provider="omlx",
            backend="network", transport_model="omlx@test/model",
            expected_reported_model=None, context_window=200_000,
            available=True, availability_reason=None,
            display_name="Test Model", supports_reasoning=False,
        )
        monkeypatch.setattr(turn_mod, "_setup_turn", fake_setup_turn)
        monkeypatch.setattr(turn_mod, "run_tracked_turn", fake_run_tracked_turn)
        monkeypatch.setattr(turn_mod, "run_post_turn_tasks", fake_post_turn_tasks)
        monkeypatch.setattr(turn_mod, "emit_product_event", lambda *_args, **_kwargs: None)
        await turn_mod.execute_turn(
            ws,
            {"id": "user-1"},
            "user-1",
            "sess-x",
            {"model": "omlx@test/model"},
            "hello",
            [],
            False,
            target=target,
            turn_id="turn-cache-warm-interrupt",
            ingress="ws",
        )
        assert not cancelled.is_set()
        assert not any(
            event.get("type") == "cache_warm_interrupted" for event in ws.sent
        )
        assert state._active_warm_tasks["sess-x"] is task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        state._active_warm_tasks.pop("sess-x", None)

    asyncio.run(_drive())
    assert cancelled.is_set()
