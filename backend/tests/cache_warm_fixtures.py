from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def _stable_imports():
    import importlib

    submodules = ("llm.providers", "llm.capabilities", "core.thread_history")
    for mod_name in submodules:
        mod = importlib.import_module(mod_name)
        parent_name, _, attr = mod_name.rpartition(".")
        parent = importlib.import_module(parent_name)
        if getattr(parent, attr, None) is not mod:
            setattr(parent, attr, mod)

def _fake_session_row(
    model: str,
    project_id: str | None = None,
    spawned_by: str | None = None,
    reasoning_effort: str | None = None,
):
    class _Row:
        def __init__(self, d):
            self._d = d

        def __getitem__(self, key):
            return self._d.get(key)

        def keys(self):
            return self._d.keys()

    return _Row({
        "model": model,
        "project_id": project_id,
        "spawned_by": spawned_by,
        "reasoning_effort": reasoning_effort,
    })


def _make_mock_agent(system_prompt: str = "SYSTEM") -> MagicMock:
    agent = MagicMock()
    agent.memory_window = 100
    agent.max_tokens = 8192
    agent._connect_mcp = AsyncMock()
    agent.tools.get_definitions = MagicMock(return_value=[])
    agent.context.build_system_prompt = MagicMock(return_value=system_prompt)
    return agent


def install_warm_endpoint_session(
    monkeypatch,
    api_runtime,
    selected_model: str,
    *,
    db=None,
    project_id: str | None = None,
    reasoning_effort: str | None = None,
):
    """Install the authenticated session boundary shared by warm tests."""
    session_db = MagicMock() if db is None else db
    monkeypatch.setattr(api_runtime, "authenticate", lambda _token: {"id": "steve"})
    monkeypatch.setattr(api_runtime, "get_db", lambda: session_db)
    monkeypatch.setattr(
        api_runtime,
        "get_thread_for_user",
        lambda _sid, _uid, db=None: _fake_session_row(
            selected_model,
            project_id,
            reasoning_effort=reasoning_effort,
        ),
    )


def install_resolved_omlx_session(
    monkeypatch,
    api_runtime,
    transport_model: str,
    *,
    context_window: int = 128000,
    reasoning_effort: str | None = None,
    supports_reasoning: bool = False,
):
    """Install one authenticated session with an already-resolved oMLX target."""
    from llm.resolution import ExecutionTarget

    provider_model_id = transport_model.partition("/")[2]
    target = ExecutionTarget(
        catalog_id=f"omlx/{provider_model_id}",
        harness="native",
        provider="omlx",
        backend="network",
        transport_model=transport_model,
        expected_reported_model=None,
        context_window=context_window,
        available=True,
        availability_reason=None,
        display_name=provider_model_id,
        supports_reasoning=supports_reasoning,
    )
    session_db = MagicMock()
    session_db.execute.return_value.fetchone.return_value = None
    install_warm_endpoint_session(
        monkeypatch,
        api_runtime,
        transport_model,
        db=session_db,
        reasoning_effort=reasoning_effort,
    )
    monkeypatch.setattr(
        api_runtime,
        "resolve_thread_target",
        lambda _selected, *, thread_id, db: target,
        raising=False,
    )


def make_exact_replay_selection(
    thread_id: str,
    turns: list[list[tuple[str, str]]],
    *,
    history_epoch: int = 1,
):
    """Build a real exact ``ReplaySelection`` over a small seeded projection.

    ``turns`` is a list of completed turns, each a list of ``(role, content)``
    pairs. The projection is the portable-history shape send preflight renders,
    so tests exercising the derived fallback seam replay the same bytes the
    live seam sends.
    """
    from core.context_replay import ReplaySelection
    from history.types import (
        HistoryBoundary,
        PortableHistoryProjection,
        PortableMessage,
        PortableTurn,
    )

    projected_turns = []
    seq = 0
    boundary = None
    for index, turn_messages in enumerate(turns, start=1):
        turn_id = f"turn-{index}"
        messages = []
        for role, content in turn_messages:
            seq += 1
            messages.append(
                PortableMessage(
                    message_id=f"{role}-{seq}",
                    seq=seq,
                    turn_id=turn_id,
                    role=role,
                    content=content,
                    created_at="t",
                    assistant_step_id=(
                        f"{role}-{seq}" if role == "assistant" else None
                    ),
                )
            )
        boundary = HistoryBoundary(
            thread_id=thread_id,
            history_epoch=history_epoch,
            seq=seq,
            message_id=messages[-1].message_id,
        )
        projected_turns.append(
            PortableTurn(
                turn_id=turn_id,
                status="completed",
                messages=tuple(messages),
                boundary=boundary,
            )
        )
    projection = PortableHistoryProjection(
        format_version=1,
        thread_id=thread_id,
        history_epoch=history_epoch,
        start_exclusive=None,
        sealed_boundary=boundary,
        turns=tuple(projected_turns),
        full_raw_replay_available=True,
    )
    return ReplaySelection(
        mode="exact",
        history_epoch=history_epoch,
        sealed_boundary=boundary,
        tail=projection,
    )


def make_checkpoint_tail_replay_selection(
    exact,
    *,
    tail_turns: int = 1,
):
    """Cover the older exact turns with a checkpoint and retain the newest tail."""
    import json

    from core.context_replay import ReplaySelection

    turns = exact.tail.turns
    if tail_turns < 1 or len(turns) <= tail_turns:
        raise ValueError("checkpoint-tail fixture needs an older covered turn")
    checkpoint_boundary = turns[-tail_turns - 1].boundary
    tail = type(exact.tail)(
        format_version=exact.tail.format_version,
        thread_id=exact.tail.thread_id,
        history_epoch=exact.tail.history_epoch,
        start_exclusive=checkpoint_boundary,
        sealed_boundary=exact.tail.sealed_boundary,
        turns=turns[-tail_turns:],
        full_raw_replay_available=True,
    )
    return ReplaySelection(
        mode="checkpoint_tail",
        history_epoch=exact.history_epoch,
        sealed_boundary=exact.sealed_boundary,
        tail=tail,
        checkpoint_generation_id="warm-generation-1",
        checkpoint_boundary=checkpoint_boundary,
        checkpoint_json=json.dumps(
            {
                "schema_version": 1,
                "reason": "pressure",
                "summary": "Covered history before the retained exact tail.",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _empty_raw_selection(history_epoch: int = 1):
    """The raw replay of a thread with no sealed history — never None."""
    from core.context_replay import ReplaySelection
    from history.types import PortableHistoryProjection

    return ReplaySelection(
        mode="exact",
        history_epoch=history_epoch,
        sealed_boundary=None,
        tail=PortableHistoryProjection(
            format_version=1,
            thread_id="replay-candidates-fixture",
            history_epoch=history_epoch,
            start_exclusive=None,
            sealed_boundary=None,
            turns=(),
            full_raw_replay_available=True,
        ),
    )


def install_replay_candidates(
    monkeypatch,
    *,
    exact=None,
    checkpoint_tail=None,
    last_resort=None,
):
    """Serve fixed replay candidates to the derived fallback seam."""
    from core.context_replay import ReplayCandidates

    reference = exact or checkpoint_tail
    candidates = ReplayCandidates(
        exact=exact,
        checkpoint_tail=checkpoint_tail,
        history_epoch=(
            reference.history_epoch if reference is not None else 1
        ),
        sealed_boundary=(
            reference.sealed_boundary if reference is not None else None
        ),
        last_resort=last_resort or exact or _empty_raw_selection(),
    )
    calls: list[str] = []

    def _read(thread_id, *, db=None, exclude_message_id=None):
        calls.append(thread_id)
        return candidates

    monkeypatch.setattr("core.context_replay.read_replay_candidates", _read)
    return calls


def install_cache_warm_isolation(monkeypatch):
    _stable_imports()
    from infra import state
    state._active_warm_tasks.clear()
    # This module binds provider_boundary_snapshot by name at import. Import it
    # before the patch, or a first import under the patch keeps the fake after
    # monkeypatch restores the source.
    import core.turn_iteration_context  # noqa: F401

    monkeypatch.setattr(
        "infra.runtime_truth.provider_boundary_snapshot",
        lambda *_args, **_kwargs: (None, None, None),
    )
    monkeypatch.setattr(
        "core.native_context_sources._context_sources_for_prefix",
        lambda *_a, **_kw: SimpleNamespace(
            installed=None, project_cwd=None,
            agent_facts={},
            sources=[],
        ),
    )
    return state


def cleanup_cache_warm_isolation(state):
    for task in list(state._active_warm_tasks.values()):
        if not task.done():
            task.cancel()
    state._active_warm_tasks.clear()


@pytest.fixture(autouse=True)
def _clear_warm_state():
    from infra import state
    _stable_imports()
    state._active_warm_tasks.clear()
    yield
    cleanup_cache_warm_isolation(state)


@pytest.fixture(autouse=True)
def _isolate_prefix_context_sources(monkeypatch):
    """Keep cache-warm unit tests independent from the live/runtime DB."""
    install_cache_warm_isolation(monkeypatch)
