"""Task-local Native checkpoint state for the agent request tool."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from core.checkpoint_agent_admission import CheckpointNotReady
from history.types import HistoryBoundary
from infra.checkpoint_admission import IDLE_MIN_DELTA_TOKENS, admits_idle_checkpoint


@dataclass(frozen=True, slots=True)
class NativeAgentCheckpointRuntime:
    """The live Native Turn facts required by native checkpoint admission."""

    state: Any
    thread_id: str
    turn_identity: Any
    reasoning_effort: str | None


_current_native_runtime: ContextVar[NativeAgentCheckpointRuntime | None] = ContextVar(
    "current_native_agent_checkpoint_runtime", default=None,
)


def bind_native_agent_checkpoint_runtime(
    state: Any,
    thread_id: str,
    turn_identity: Any,
    reasoning_effort: str | None,
):
    """Bind the live Native Turn only while its tools execute."""
    return _current_native_runtime.set(
        NativeAgentCheckpointRuntime(state, thread_id, turn_identity, reasoning_effort)
    )


def reset_native_agent_checkpoint_runtime(token) -> None:
    """Clear the current Turn's Native checkpoint runtime binding."""
    _current_native_runtime.reset(token)


@contextmanager
def bound_native_agent_checkpoint_runtime(
    state: Any, thread_id: str, turn_identity: Any, reasoning_effort: str | None,
):
    """Scope Native checkpoint admission to the tool-execution portion of one Turn."""
    token = bind_native_agent_checkpoint_runtime(
        state, thread_id, turn_identity, reasoning_effort,
    )
    try:
        yield
    finally:
        reset_native_agent_checkpoint_runtime(token)


async def prepare_native_agent_checkpoint():
    """Prepare one minimum-qualified agent checkpoint through Native admission."""
    runtime = _current_native_runtime.get()
    if runtime is None:
        return None
    if runtime.turn_identity is None or runtime.state.fit_ledger is None:
        raise CheckpointNotReady(
            "native checkpoint context is unavailable", reason_key="no_sealed_boundary",
        )
    from core.fork_fit import foldable_tail_tokens
    from core.checkpoint_native_admission import prepare_native_checkpoint
    from core.fork_types import NothingNewerToFold
    from core.turn_iteration_context import _read_native_checkpoint_context
    from infra.db_async import run_in_db

    try:
        context = await run_in_db(
            _read_native_checkpoint_context,
            runtime.thread_id,
            runtime.state.fit_ledger.target_catalog_id,
        )
    except NothingNewerToFold:
        context = None
    if context is None:
        raise CheckpointNotReady(
            "checkpoint requires a sealed message boundary", reason_key="no_sealed_boundary",
        )
    previous_boundary = (
        None if context.previous is None
        else HistoryBoundary.of_checkpoint(context.previous)
    )
    foldable_tokens = foldable_tail_tokens(
        context.projection,
        previous_boundary,
        model=context.target.expected_reported_model or context.target.transport_model,
    )
    if not admits_idle_checkpoint(foldable_tokens):
        raise CheckpointNotReady(
            "not enough new material to be worth a paid fold",
            reason_key="below_minimum",
            foldable_tokens=foldable_tokens,
            threshold_tokens=IDLE_MIN_DELTA_TOKENS,
        )
    return await prepare_native_checkpoint(
        str(uuid4()),
        runtime.thread_id,
        runtime.state,
        context,
        runtime.turn_identity,
        runtime.reasoning_effort,
        trigger="agent",
    )
