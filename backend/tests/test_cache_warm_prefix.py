from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cache_warm_fixtures import (
    cleanup_cache_warm_isolation,
    install_cache_warm_isolation,
    install_replay_candidates,
    install_resolved_omlx_session,
    make_checkpoint_tail_replay_selection,
    make_exact_replay_selection,
    _make_mock_agent,
)


@pytest.fixture(autouse=True)
def _cache_warm_isolation(monkeypatch):
    state = install_cache_warm_isolation(monkeypatch)
    yield
    cleanup_cache_warm_isolation(state)


_CONVERSATION_TURNS = [
    [
        ("user", "What's the second law of thermodynamics?"),
        ("assistant", "Entropy of an isolated system never decreases."),
    ],
    [
        ("user", "Can you elaborate with an example?"),
        ("assistant", "Sure — consider heat flow from hot to cold..."),
    ],
    [
        ("user", "Tell me about Gibbs free energy."),
        ("assistant", "Gibbs free energy G = H - TS measures usable work."),
    ],
]


def _oversized_exact_with_checkpoint_tail(thread_id: str):
    exact = make_exact_replay_selection(
        thread_id,
        [
            [("user", "x" * 120_000), ("assistant", "y" * 120_000)],
            [("user", "retained tail"), ("assistant", "retained answer")],
        ],
    )
    return exact, make_checkpoint_tail_replay_selection(exact)


def test_cache_warm_eager_checkpoint_matches_native_send_without_exact_render(
    monkeypatch,
):
    from api import thread_cache_warm as api_runtime
    from core.native_context_prefix import build_real_send_prefix

    exact = make_exact_replay_selection("sess-warm-eager", _CONVERSATION_TURNS)
    checkpoint_tail = make_checkpoint_tail_replay_selection(exact)
    install_replay_candidates(
        monkeypatch, exact=exact, checkpoint_tail=checkpoint_tail,
    )
    connection = MagicMock()
    monkeypatch.setattr(api_runtime, "get_db", lambda: connection)
    eager = SimpleNamespace(id=checkpoint_tail.checkpoint_generation_id)
    snapshot = MagicMock(return_value=(None, eager, eager))
    monkeypatch.setattr(
        "infra.runtime_truth.provider_boundary_snapshot", snapshot,
    )
    monkeypatch.setattr(
        "infra.db_async.run_in_db",
        AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
    )
    agent = _make_mock_agent(system_prompt="<SYS>")
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=agent))
    tools = ({
        "type": "function",
        "function": {
            "name": "lookup",
            "parameters": {"type": "object", "properties": {}},
        },
    },)
    monkeypatch.setattr("tools.catalog.advertised_tool_definitions", lambda _tools: tools)
    observed: list[str] = []

    async def observe(**kwargs):
        observed.append(kwargs["replay_selection"].mode)
        return await build_real_send_prefix(**kwargs)

    monkeypatch.setattr("core.native_context_prefix.build_real_send_prefix", observe)
    target = SimpleNamespace(
        catalog_id="omlx/model-1", transport_model="omlx@srv-1/model-1",
        context_window=128_000, preserve_thinking_default=False,
    )
    warm_messages, warm_tools, _shape = asyncio.run(
        api_runtime._build_warm_prefix("sess-warm-eager", None, target, None)
    )
    send_messages, _info = asyncio.run(build_real_send_prefix(
        agent=agent, thread_id="sess-warm-eager",
        turn_model=target.transport_model, fresh_context=False, project_id=None,
        model_context_window=target.context_window,
        replay_selection=checkpoint_tail, tool_definitions=tools,
    ))

    assert observed == ["checkpoint_tail"]
    assert warm_messages == send_messages
    assert tuple(warm_tools) == tools
    assert connection.execute.call_args_list == [
        (("BEGIN",), {}),
        (("ROLLBACK",), {}),
    ]
    snapshot.assert_called_once_with(
        "sess-warm-eager", "native", target.catalog_id, db=connection,
    )


def test_cache_warm_selects_fitting_checkpoint_tail_after_exact_exceeds_budget(
    monkeypatch,
):
    from api import thread_cache_warm as api_runtime
    from core.native_context_prefix import build_real_send_prefix

    exact, checkpoint_tail = _oversized_exact_with_checkpoint_tail("sess-warm-fit")
    install_replay_candidates(
        monkeypatch,
        exact=exact,
        checkpoint_tail=checkpoint_tail,
    )
    monkeypatch.setattr(api_runtime, "get_db", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(
        "infra.db_async.run_in_db",
        AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
    )
    agent = _make_mock_agent(system_prompt="<SYS>")
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr("tools.catalog.advertised_tool_definitions", lambda _tools: [])
    observed_info = []

    async def _observe_prefix(**kwargs):
        result = await build_real_send_prefix(**kwargs)
        observed_info.append(result[1])
        return result

    monkeypatch.setattr(
        "core.native_context_prefix.build_real_send_prefix",
        _observe_prefix,
    )
    target = SimpleNamespace(
        catalog_id="omlx/model-1",
        transport_model="omlx@srv-1/model-1",
        context_window=60_000,
        preserve_thinking_default=False,
    )

    messages, _tools, _shape = asyncio.run(
        api_runtime._build_warm_prefix("sess-warm-fit", None, target, None)
    )

    assert observed_info[-1]["continuity_mode"] == "checkpoint_tail"
    assert "[CORSIA checkpoint]" in messages[0]["content"]
    assert all("x" * 1_000 not in str(message) for message in messages)


def test_cache_warm_adopts_checkpoint_even_when_archive_would_fit(monkeypatch):
    from api import thread_cache_warm as api_runtime
    from core.context_replay import ReplayCandidates
    from core.destination_preflight import attach_native_provider_fit_ledger

    exact = make_exact_replay_selection(
        "sess-warm-near-full",
        [
            [("user", "x" * 25_500), ("assistant", "y" * 25_500)],
            [("user", "retained tail"), ("assistant", "retained answer")],
        ],
    )
    checkpoint_tail = make_checkpoint_tail_replay_selection(exact)
    candidates = ReplayCandidates(
        exact=exact,
        checkpoint_tail=checkpoint_tail,
        history_epoch=exact.history_epoch,
        sealed_boundary=exact.sealed_boundary,
        last_resort=checkpoint_tail,
    )
    monkeypatch.setattr(
        "infra.db_async.run_in_db",
        AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
    )
    agent = _make_mock_agent(system_prompt="<SYS>")
    target = SimpleNamespace(
        catalog_id="omlx/model-1",
        transport_model="omlx@srv-1/model-1",
        context_window=60_000,
        preserve_thinking_default=False,
    )

    messages, info = asyncio.run(
        api_runtime._render_warm_replay(
            agent=agent,
            thread_id="sess-warm-near-full",
            project_id=None,
            target=target,
            candidates=candidates,
            eager_checkpoint_id=None,
            tool_defs=(),
        )
    )
    ledger = attach_native_provider_fit_ledger(
        target_catalog_id=target.catalog_id,
        proven_target_window_tokens=target.context_window,
        messages=messages,
        tools=[],
        input_identity="warm:sess-warm-near-full",
        current_input=None,
    ).fit_ledger

    assert ledger is not None
    assert ledger.rendered_input_tokens < target.context_window * 0.80
    assert ledger.fits is True
    assert info["continuity_mode"] == "checkpoint_tail"
    assert "[CORSIA checkpoint]" in messages[0]["content"]
    assert all("x" * 1_000 not in str(message) for message in messages)


def test_cache_warm_fails_closed_when_only_exact_history_exceeds_budget(monkeypatch):
    from api import thread_cache_warm as api_runtime

    exact, _checkpoint_tail = _oversized_exact_with_checkpoint_tail(
        "sess-warm-no-fit"
    )
    install_replay_candidates(monkeypatch, exact=exact)
    monkeypatch.setattr(api_runtime, "get_db", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(
        "infra.db_async.run_in_db",
        AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
    )
    agent = _make_mock_agent(system_prompt="<SYS>")
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr("tools.catalog.advertised_tool_definitions", lambda _tools: [])
    target = SimpleNamespace(
        catalog_id="omlx/model-1",
        transport_model="omlx@srv-1/model-1",
        context_window=60_000,
        preserve_thinking_default=False,
    )

    with pytest.raises(ValueError, match="input exceeds the selected model"):
        asyncio.run(
            api_runtime._build_warm_prefix("sess-warm-no-fit", None, target, None)
        )


def test_build_real_send_prefix_byte_equality_warm_vs_real_send(monkeypatch):
    from core.native_context_prefix import build_real_send_prefix

    selection = make_exact_replay_selection("sess-byte-eq", _CONVERSATION_TURNS)
    install_replay_candidates(monkeypatch, exact=selection)
    monkeypatch.setattr(
        "infra.db_async.run_in_db",
        AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
    )
    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda m: 128000,
    )

    mock_agent = _make_mock_agent(system_prompt="<SYSTEM-PROMPT-FOR-CHAT-ROLE>")

    async def _build():
        return await build_real_send_prefix(
            agent=mock_agent,
            thread_id="sess-byte-eq",
            turn_model="omlx@srv-1/Local-Model-A3B-8bit",
            fresh_context=False,
            project_id=None,
            replay_selection=selection,
        )

    prefix_a, info_a = asyncio.run(_build())
    prefix_b, info_b = asyncio.run(_build())

    assert prefix_a == prefix_b, (
        "build_real_send_prefix is non-deterministic — same inputs produced "
        "different prefix bytes. This bricks oMLX's strict-prefix cache."
    )
    assert info_a == info_b
    assert info_a["continuity_mode"] == "exact"

    assert prefix_a[0]["role"] == "system"
    assert prefix_a[0]["content"] == "<SYSTEM-PROMPT-FOR-CHAT-ROLE>"
    assert len(prefix_a) == 1 + sum(len(turn) for turn in _CONVERSATION_TURNS)
    assert prefix_a[1]["role"] == "user"
    assert prefix_a[1]["content"] == _CONVERSATION_TURNS[0][0][1]


def test_edited_resend_serializes_unchanged_history_and_tools_as_prefix(monkeypatch):
    """Only the edited input moves; prior history and canonical tools stay reusable."""
    from core.turn_context import prepare_turn_context

    selection = make_exact_replay_selection("sess-edited", _CONVERSATION_TURNS)
    install_replay_candidates(monkeypatch, exact=selection)
    monkeypatch.setattr(
        "infra.db_async.run_in_db",
        AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
    )
    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 128000,
    )
    canonical_tools = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    agent = _make_mock_agent(system_prompt="<SYSTEM-PROMPT-FOR-EDIT>")

    async def _render(user_content):
        return await prepare_turn_context(
            agent=agent,
            thread_id="sess-edited",
            user_content=user_content,
            turn_model="omlx@srv-1/Local-Model-A3B-8bit",
            fresh_context=False,
            project_id=None,
            channel="web",
            chat_id="sess-edited",
            media=None,
            replay_selection=selection,
            tool_definitions=canonical_tools,
        )

    original_messages, original_usage = asyncio.run(_render("original final input"))
    edited_messages, edited_usage = asyncio.run(_render("edited final input"))

    assert original_messages[:-1] == edited_messages[:-1]
    assert original_messages[-1]["content"] == "original final input"
    assert edited_messages[-1]["content"] == "edited final input"

    for messages, usage in (
        (original_messages, original_usage),
        (edited_messages, edited_usage),
    ):
        prefix_artifact = next(
            artifact
            for artifact in usage["source_artifacts"]
            if artifact["method"] == "native-prefix"
        )
        turn_artifact = next(
            artifact
            for artifact in usage["source_artifacts"]
            if artifact["method"] == "native-turn-messages"
        )
        assert prefix_artifact["payload"]["messages"] == messages[:-1]
        assert prefix_artifact["payload"]["tools"] == canonical_tools
        assert turn_artifact["payload"]["tools"] == canonical_tools


def test_near_tail_rerun_reuses_the_entire_retained_native_prefix(monkeypatch):
    """Removing the edited answer shortens, but never changes, cached bytes."""
    from core.native_context_prefix import build_real_send_prefix

    full = make_exact_replay_selection("sess-tail-edit", _CONVERSATION_TURNS)
    retained = make_exact_replay_selection(
        "sess-tail-edit", _CONVERSATION_TURNS[:-1],
    )
    monkeypatch.setattr(
        "infra.db_async.run_in_db",
        AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
    )
    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 128000,
    )
    agent = _make_mock_agent(system_prompt="<UNCHANGED-SYSTEM>")

    async def render(selection):
        return await build_real_send_prefix(
            agent=agent,
            thread_id="sess-tail-edit",
            turn_model="omlx@srv-1/Local-Model-A3B-8bit",
            fresh_context=False,
            project_id=None,
            replay_selection=selection,
        )

    full_prefix, _ = asyncio.run(render(full))
    retained_prefix, _ = asyncio.run(render(retained))

    assert full_prefix[: len(retained_prefix)] == retained_prefix
    assert len(full_prefix) > len(retained_prefix)


def test_explicit_checkpoint_tail_renders_only_covered_history(monkeypatch):
    import json as _json

    from core.context_replay import ReplaySelection
    from core.native_context_prefix import build_real_send_prefix
    from core.turn_context import prepare_turn_context

    exact = make_exact_replay_selection("sess-tail-derive", _CONVERSATION_TURNS)
    checkpoint_boundary = exact.tail.turns[1].boundary
    tail_projection = type(exact.tail)(
        format_version=exact.tail.format_version,
        thread_id=exact.tail.thread_id,
        history_epoch=exact.tail.history_epoch,
        start_exclusive=checkpoint_boundary,
        sealed_boundary=exact.tail.sealed_boundary,
        turns=exact.tail.turns[2:],
        full_raw_replay_available=True,
    )
    checkpoint_tail = ReplaySelection(
        mode="checkpoint_tail",
        history_epoch=exact.history_epoch,
        sealed_boundary=exact.sealed_boundary,
        tail=tail_projection,
        checkpoint_generation_id="generation-7",
        checkpoint_boundary=checkpoint_boundary,
        checkpoint_json=_json.dumps(
            {
                "schema_version": 1,
                "reason": "test",
                "summary": "Earlier thermodynamics discussion.",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    monkeypatch.setattr(
        "infra.db_async.run_in_db",
        AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
    )
    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda m: 128000,
    )
    mock_agent = _make_mock_agent(system_prompt="<SYS>")

    async def _build():
        return await build_real_send_prefix(
            agent=mock_agent,
            thread_id="sess-tail-derive",
            turn_model="omlx@srv-1/m",
            fresh_context=False,
            project_id=None,
            replay_selection=checkpoint_tail,
        )

    prefix, info = asyncio.run(_build())

    assert info["continuity_mode"] == "checkpoint_tail"
    assert info["checkpoint_generation_id"] == "generation-7"
    assert "[CORSIA checkpoint]" in prefix[0]["content"]
    assert "Earlier thermodynamics discussion." in prefix[0]["content"]
    # Only the tail turns replay as typed history after the checkpoint.
    assert [m["role"] for m in prefix[1:]] == ["user", "assistant"]
    assert prefix[1]["content"] == _CONVERSATION_TURNS[2][0][1]

    _messages, usage = asyncio.run(prepare_turn_context(
        mock_agent,
        "sess-tail-derive",
        "continue",
        "omlx@srv-1/m",
        False,
        None,
        "web",
        "sess-tail-derive",
        None,
        replay_selection=checkpoint_tail,
    ))
    assert usage["checkpoint_generation_id"] == "generation-7"


@pytest.mark.parametrize(
    ("target_window", "reported_window"),
    [(123_456, 123_456), (None, 0)],
)
def test_build_real_send_prefix_uses_resolved_target_context_window(
    monkeypatch, target_window, reported_window,
):
    """A managed native turn must not re-resolve its model window downstream."""
    from core.native_context_prefix import build_real_send_prefix

    selection = make_exact_replay_selection(
        "sess-target-window", _CONVERSATION_TURNS,
    )
    monkeypatch.setattr(
        "infra.db_async.run_in_db",
        AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
    )
    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: (_ for _ in ()).throw(
            AssertionError("managed turn re-resolved context window")
        ),
    )

    mock_agent = _make_mock_agent(system_prompt="SYS")

    async def _build():
        return await build_real_send_prefix(
            agent=mock_agent,
            thread_id="sess-target-window",
            turn_model="omlx@srv-1/qwen",
            fresh_context=False,
            project_id=None,
            model_context_window=target_window,
            replay_selection=selection,
        )

    _, info = asyncio.run(_build())

    assert info["model_context_window"] == reported_window


def test_warm_and_real_send_produce_strict_prefix_match(monkeypatch):
    """Warm bytes equal real-send bytes through the one shared replay path."""
    from api import thread_cache_warm as api_runtime
    from core.turn_context import prepare_turn_context

    conversation = [
        [
            ("user", "Hi there"),
            ("assistant", "Hello! How can I help?"),
        ],
        [
            ("user", "Tell me about MoE models"),
            ("assistant", "Mixture-of-Experts routes tokens to specialist sub-networks."),
        ],
    ]
    selection = make_exact_replay_selection("sess-eq", conversation)
    reads = install_replay_candidates(monkeypatch, exact=selection)

    install_resolved_omlx_session(
        monkeypatch,
        api_runtime,
        "omlx@srv-1/Local-Model-A3B-8bit",
        context_window=128000,
    )
    monkeypatch.setattr(
        "infra.db_async.run_in_db",
        AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
    )
    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda m: 128000,
    )

    SHARED_SYSTEM_PROMPT = "<SYS>shared system prompt bytes for cache discipline</SYS>"
    mock_agent = _make_mock_agent(system_prompt=SHARED_SYSTEM_PROMPT)
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=mock_agent))

    captured_warm = {}
    async def _capture_warm(cfg, *, model_id, messages, tools=None):
        captured_warm["messages"] = messages
        captured_warm["tools"] = tools
        return {"ok": True}
    fake_provider = MagicMock()
    fake_provider.warm_cache = _capture_warm
    fake_provider.probe_cache = AsyncMock(return_value={"ok": False})
    monkeypatch.setattr("llm.providers.for_provider", lambda _n: fake_provider)

    async def _run_warm():
        await api_runtime.warm_session_cache("sess-eq", token="t")
        from infra import state
        pending = state._active_warm_tasks.get("sess-eq")
        if pending is not None:
            await asyncio.wait_for(asyncio.shield(pending), timeout=2)

    asyncio.run(_run_warm())

    async def _run_real():
        return await prepare_turn_context(
            agent=mock_agent,
            thread_id="sess-eq",
            user_content="What about sparse activation?",
            turn_model="omlx@srv-1/Local-Model-A3B-8bit",
            fresh_context=False,
            project_id=None,
            channel="web",
            chat_id="sess-eq",
            media=None,
            replay_selection=selection,
        )

    real_messages, _ctx_usage = asyncio.run(_run_real())

    warm_messages = captured_warm["messages"]

    # Both sides consumed the derived replay selection — not the legacy rebuild.
    assert reads == ["sess-eq"]
    assert len(warm_messages) == 1 + sum(len(turn) for turn in conversation)
    assert len(warm_messages) == len(real_messages) - 1, (
        f"Warm produced {len(warm_messages)} msgs, real-send produced "
        f"{len(real_messages)}. Difference must be exactly 1 (the current "
        f"user message). Anything else means divergent history construction."
    )
    for i, (w, r) in enumerate(zip(warm_messages, real_messages)):
        assert w == r, (
            f"Warm and real-send prefix diverge at index {i}: "
            f"warm={w!r} vs real_send={r!r}. This breaks oMLX's strict-prefix cache."
        )

    assert real_messages[-1]["role"] == "user"
    assert real_messages[-1]["content"] == "What about sparse activation?"


def test_build_real_send_prefix_trim_uses_fixed_msg_reservation(monkeypatch):
    from core.native_context_prefix import EXPECTED_USER_MSG_RESERVATION_TOKENS, build_real_send_prefix

    assert isinstance(EXPECTED_USER_MSG_RESERVATION_TOKENS, int)
    assert EXPECTED_USER_MSG_RESERVATION_TOKENS > 0

    import inspect
    sig = inspect.signature(build_real_send_prefix)
    param_names = set(sig.parameters.keys())
    assert "user_content" not in param_names, (
        "build_real_send_prefix must NOT accept user_content — the trim "
        "decision must be deterministic across warm + real-send paths "
        "(issue #107). If user_content is passed in, the warm path would "
        "have to pre-guess the message size and the byte-equality invariant breaks."
    )


def _collection_with_owner_map(marker: str):
    """A context-source collection carrying the stable responsibility map."""
    from core.effective_context import ContextSourceEntry

    return SimpleNamespace(
        installed=None, project_cwd=None,
        agent_facts={},
        sources=[
            ContextSourceEntry(
                source_id="role:agent-owner-map",
                origin_section="role",
                label="Agent responsibility map",
                stable_reference="thread://sess/agents",
                freshness="fresh",
                inclusion="included",
                reason="current responsibility map",
                owner_surface="agents",
                prompt_content=marker,
                metadata={"provider": "agent_owner_map", "agent_count": 1},
            ),
        ],
    )


def _install_common_mocks(monkeypatch):
    monkeypatch.setattr(
        "infra.db_async.run_in_db",
        AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
    )
    monkeypatch.setattr("llm.capabilities.get_model_context_window", lambda m: 128000)
    monkeypatch.setattr(
        "core.thread_history.compute_memory_window",
        lambda model, sid, fallback: 250,
    )


def test_native_owner_map_rides_prefix_not_tail(monkeypatch):
    """The responsibility map is part of Native's stable cached prefix."""
    from types import SimpleNamespace as _SN  # noqa: F401 (SimpleNamespace already imported)
    from core.native_context_prefix import build_real_send_prefix

    _install_common_mocks(monkeypatch)
    STATIC = "<SYSTEM STATIC PREFIX>"
    mock_agent = _make_mock_agent(system_prompt=STATIC)

    async def _build(marker):
        monkeypatch.setattr(
            "core.native_context_sources._context_sources_for_prefix",
            lambda *a, **kw: _collection_with_owner_map(marker),
        )
        return await build_real_send_prefix(
            agent=mock_agent,
            thread_id="sess-tail",
            turn_model="omlx@srv-1/m",
            fresh_context=True,
            project_id=None,
        )

    prefix_a, _ = asyncio.run(_build("| Agent |\n| Builder A |"))
    prefix_b, _ = asyncio.run(_build("| Agent |\n| Builder B |"))

    assert prefix_a[0]["role"] == "system"
    assert STATIC in prefix_a[0]["content"]
    assert "Builder A" in prefix_a[0]["content"]
    assert prefix_a[0]["content"] != prefix_b[0]["content"]


def test_native_static_change_changes_rendered_prefix(monkeypatch):
    """A static bootloader change is visible in the next rendered prefix."""
    from core.native_context_prefix import build_real_send_prefix

    _install_common_mocks(monkeypatch)
    monkeypatch.setattr(
        "core.native_context_sources._context_sources_for_prefix",
        lambda *a, **kw: _collection_with_owner_map("| Agent |\n| Builder |"),
    )

    async def _build(system_prompt):
        agent = _make_mock_agent(system_prompt=system_prompt)
        return await build_real_send_prefix(
            agent=agent,
            thread_id="sess-static",
            turn_model="omlx@srv-1/m",
            fresh_context=True,
            project_id=None,
        )

    prefix_a, _ = asyncio.run(_build("<STATIC v1>"))
    prefix_b, _ = asyncio.run(_build("<STATIC v2 — edited bootloader>"))

    assert prefix_a[0]["content"] != prefix_b[0]["content"]


def test_native_owner_map_delivered_in_prefix_and_counted(monkeypatch):
    """The responsibility map is visible once and counted as prefix context."""
    from core.turn_context import prepare_turn_context

    _install_common_mocks(monkeypatch)
    monkeypatch.setattr(
        "core.native_context_sources._context_sources_for_prefix",
        lambda *a, **kw: _collection_with_owner_map("| Agent |\n| ROUTER |"),
    )
    mock_agent = _make_mock_agent(system_prompt="<STATIC>")

    async def _run():
        return await prepare_turn_context(
            agent=mock_agent,
            thread_id="sess-deliver",
            user_content="do the thing",
            turn_model="omlx@srv-1/m",
            fresh_context=True,
            project_id=None,
            channel="web",
            chat_id="sess-deliver",
            media=None,
        )

    messages, usage = asyncio.run(_run())

    user_msg = messages[-1]
    assert user_msg["role"] == "user"
    assert user_msg["content"] == "do the thing"
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].count("ROUTER") == 1

    # Counted: exact aggregate owns the bytes; source row remains attribution proof.
    inventory = usage["context_snapshot"]["source_inventory"]
    aggregate = next(
        s for s in inventory if s["source_id"] == "native:system:agent-owner-map"
    )
    proof = next(s for s in inventory if s["source_id"] == "role:agent-owner-map")
    assert aggregate["prompt_tokens"] and aggregate["prompt_tokens"] > 0
    assert proof["inclusion"] == "proof"
