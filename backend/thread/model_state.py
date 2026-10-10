"""Model-switch value types and the sole in-process request generation gate."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


class ThreadQuiescenceError(RuntimeError):
    """A destructive boundary could not prove all exact runtimes stopped."""


class ModelSwitchPreparationError(RuntimeError):
    """A model switch could not prove a complete destination payload."""


class ModelSwitchCheckpointRequired(ModelSwitchPreparationError):
    """No already-valid projection fits; paid checkpoint consent is required."""

    def __init__(
        self,
        current_model: str,
        target_model: str,
        *,
        reasons: tuple[str, ...] = (),
    ) -> None:
        self.current_model = current_model
        self.target_model = target_model
        self.reasons = reasons
        super().__init__(
            "model switch requires a checkpoint before the destination can fit"
        )


class ModelSwitchConflict(RuntimeError):
    """A prepared model switch lost its final compare-and-swap."""


class ModelSwitchSequencingRequired(ModelSwitchConflict):
    """The durable boundary moved under this preparation; re-read it once.

    Append-only Turn/input work, or another driver of the same request, won
    the CAS. The selected boundary is still the authority, so the caller
    re-reads it instead of failing the switch on a stale snapshot.
    """


class ModelSwitchSuperseded(ModelSwitchConflict):
    """A newer in-process preparation already owns this selection's progress."""


@dataclass(frozen=True, slots=True)
class ModelSwitchInputProof:
    """Compact selected FIFO facts, separate from attachment rendering."""

    operation_id: str
    execution_epoch: int
    members: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True, slots=True)
class ModelSwitchPreparation:
    """Immutable destination proof captured before the guarded model update."""

    thread_id: str
    user_id: str | None
    generation: int
    source_model: str
    source_selected_model: str | None
    source_harness: str | None
    destination_model: str
    destination_target: Any
    project_id: str | None
    source_runtime_context_epoch: int
    source_runtime_owner: Any | None
    history_epoch: int
    sealed_boundary: Any
    destructive_version: tuple[int, int, int]
    transcript_version: tuple[int, int, int, int]
    source_active_turn_id: str | None
    projection_mode: str
    projection_proof: ModelSwitchProjectionProof
    destination_fit_ledger: Any
    # Exact pre-retarget measured occupancy. The first destination turn checks
    # this against its normalized provider input before trusting continuation.
    source_context_usage_floor: int | float | None = None
    input_proof: ModelSwitchInputProof | None = None


@dataclass(frozen=True, slots=True)
class ModelSwitchProjectionProof:
    """Exact installed checkpoint identity, when one was rendered."""

    mode: str
    checkpoint_id: str | None
    checkpoint_boundary: Any


@dataclass(frozen=True, slots=True)
class ModelSwitchOutcome:
    """Public result for the single prepare/commit/post-effects path."""

    changed: bool
    old_model: str | None
    new_model: str
    preparation: ModelSwitchPreparation | None
    breadcrumb: dict | None
    committed_at: str | None = None
    post_effects_error: str | None = None


@dataclass(frozen=True, slots=True)
class ModelSwitchCommit:
    """Truth produced atomically by the guarded model update."""

    breadcrumb: dict | None
    committed_at: str


ModelSwitchRenderer = Callable[..., Awaitable[Any]]

_model_switch_thread_gates: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, asyncio.Lock],
] = weakref.WeakKeyDictionary()
_model_switch_generations: dict[str, int] = {}


def _model_switch_gate(thread_id: str) -> asyncio.Lock:
    """Return the in-process generation/CAS gate for one thread."""
    loop = asyncio.get_running_loop()
    by_thread = _model_switch_thread_gates.setdefault(loop, {})
    return by_thread.setdefault(thread_id, asyncio.Lock())


def _advance_model_switch_generation(thread_id: str) -> int:
    generation = _model_switch_generations.get(thread_id, 0) + 1
    _model_switch_generations[thread_id] = generation
    return generation


def _require_current_model_switch_generation(
    thread_id: str,
    generation: int,
) -> None:
    if _model_switch_generations.get(thread_id) != generation:
        raise ModelSwitchSuperseded(
            "model switch preparation was superseded by a newer generation"
        )


async def _run_successful_model_switch_request(
    thread_id: str,
    request: Callable[[], Awaitable[Any]],
) -> Any:
    """Serialize one request and supersede older preparations only on success."""
    gate = _model_switch_gate(thread_id)
    async with gate:
        result = await request()
        _advance_model_switch_generation(thread_id)
        return result
