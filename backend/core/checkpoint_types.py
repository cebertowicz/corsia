"""Value objects for the clean append-only checkpoint lifecycle."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, TypeAlias

from core.fork_types import FORK_OUTPUT_TOKENS, ForkNotAdmitted

CheckpointTrigger = Literal["manual", "pressure", "model_switch", "idle", "agent"]
ColdInputCallback: TypeAlias = Callable[[int | None], Awaitable[None]]

# Exact diagnostic distinguishes degraded completion from user Stop/failure.
CHECKPOINT_CONTEXT_TRUNCATED = "checkpoint_context_truncated"
# Output every checkpoint call reserves; soft pressure leaves room for it.
CHECKPOINT_OUTPUT_TOKENS = FORK_OUTPUT_TOKENS
# Covers a fork's tool-call reply and a short error; a larger result is refused.
CHECKPOINT_TOOL_ROUND_TRIP_TOKENS = 4_096


class CheckpointContinuationError(RuntimeError):
    """The selected provider cannot safely make its next call."""


class CheckpointUnavailable(CheckpointContinuationError):
    """Unwritten input must wait for a change to its checkpoint or cap."""

    def __init__(
        self, detail: str, *, context_window: int,
        installed_checkpoint_id: str | None,
        limited_target: str | None = None,
        limited_connection: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.context_window = context_window
        self.installed_checkpoint_id = installed_checkpoint_id
        # Set only when the fold's own target was account-limited at the gate.
        self.limited_target = limited_target
        self.limited_connection = limited_connection



class CheckpointTargetLimited(ForkNotAdmitted):
    """The fold's own target connection is account-limited; no provider call was made."""

    def __init__(self, held) -> None:
        from infra.thread_input_types import held_admission_line

        super().__init__(held_admission_line(held))
        self.model = held.model
        self.connection = held.connection


#: What a person is told when a checkpoint is refused because the project has cloud models off.
CLOUD_OFF_CHECKPOINT_MESSAGE = (
    "This project uses local models only. Switch this chat to a local model to checkpoint it."
)


class OptionalCheckpointSkipped(Exception):
    """A preparer declined an optional fold by design; not an admission failure."""


#: A same-target install replaces its covered prefix at the next safe boundary.
#: Idle capture is preparation too: its eventual cutover is intentional, not
#: contingent on the old cache expiring. A model-switch fold belongs to the
#: destination switch owner and must not force the source to adopt it.
EAGER_ADOPTION_TRIGGERS: tuple[CheckpointTrigger, ...] = (
    "manual", "pressure", "idle", "agent",
)

#: Retire durable source registration on install; a running Turn finishes its
#: accepted Step and uses the existing checkpoint continuation beneath that Turn.
SOURCE_RETIRING_TRIGGERS: tuple[CheckpointTrigger, ...] = EAGER_ADOPTION_TRIGGERS

CheckpointMode = Literal["warm", "cold"]
CheckpointStatus = Literal[
    "running",
    "captured",
    "installed",
    "overtaken",
    "failed",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class CheckpointRequest:
    """Admission identity; cold input's fitted start is recorded before generation."""

    id: str
    thread_id: str
    history_epoch: int
    previous_generation_id: str | None
    through_seq: int
    through_message_id: str
    trigger: CheckpointTrigger
    mode: CheckpointMode
    source_harness: str
    source_model: str
    writer_harness: str
    writer_model: str
    originating_turn_id: str | None
    first_included_seq: int | None
    started_at: str


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    """One retained checkpoint operation and its exact durable result."""

    generation_order: int
    id: str
    thread_id: str
    history_epoch: int
    previous_generation_id: str | None
    through_seq: int
    through_message_id: str
    trigger: CheckpointTrigger
    mode: CheckpointMode
    status: CheckpointStatus
    source_harness: str
    source_model: str
    writer_harness: str
    writer_model: str
    originating_turn_id: str | None
    first_included_seq: int | None
    continuity_base_revision: int
    continuity_base_snapshot_json: str
    raw_result: str | None
    continuity_snapshot_json: str | None
    #: Frozen at install (#1602). NULL before this change and whenever capture
    #: did not run; written once inside the install transaction, never after.
    artifact_snapshot_json: str | None
    plan_status_snapshot_json: str | None
    #: Frozen at install (#3196): the creation time of the boundary message the
    #: narrative covers. NULL before this change and on any row whose boundary
    #: was not durable at install; the rendered checkpoint dates its history
    #: from it, so a reader can tell borrowed history from a fresh instruction.
    through_at: str | None
    usage_json: str | None
    error: str | None
    started_at: str
    captured_at: str | None
    installed_at: str | None
    ended_at: str | None


@dataclass(frozen=True, slots=True)
class CheckpointSummary:
    """The bounded public/status projection of one checkpoint operation."""

    generation_order: int
    id: str
    trigger: CheckpointTrigger
    mode: CheckpointMode
    status: CheckpointStatus
    source_model: str
    writer_model: str
    error: str | None
    started_at: str


class IdleCheckpointDisposition(str, Enum):
    """Outcome of consuming one producer-owned idle checkpoint marker."""

    ADMITTED = "admitted"
    COALESCED = "coalesced"
    FAILED = "failed"
    REFUSED = "refused"
    STALE = "stale"


class IdleCheckpointReason(str, Enum):
    """Canonical, queryable `reason` property for one idle marker outcome.

    The sole home for these literals: every idle_checkpoint event (fired,
    skipped, excluded or expired) sets `properties["reason"]` from this enum
    instead of a freestanding string, so one query over the family covers the
    whole vocabulary and it cannot drift by call site.
    """

    FIRED = "fired"
    BELOW_MINIMUM = "below_minimum"
    NO_BOUNDARY = "no_boundary"
    TURN_RUNNING = "turn_running"
    ALREADY_COVERED = "already_covered"
    # The Turn's model resolved to no available execution target.
    TARGET_UNAVAILABLE = "target_unavailable"
    # No warm prefix was proved through the boundary: a harness capture was
    # refused, or a native receipt was missing, ended elsewhere, or overflowed.
    WARM_CAPTURE_REFUSED = "warm_capture_refused"
    STALE = "stale"
    COALESCED = "coalesced"
    # Producer-side exclusions: no marker is ever armed.
    CHILD = "child"
    ARCHIVED = "archived"
    # A native thread whose transport routes to the shared local engine
    # (oMLX): a local fold would compete with foreground local turns, and
    # no local admission path defers it, so it stays excluded (#2283).
    LOCAL_SHARED_MODEL = "local_shared_model"
    # Locality policy forbids the thread's own model (the skip's ``cause`` names which,
    # e.g. project_cloud_off), so no background fold may send its history there.
    LOCALITY = "locality"
    UNRESOLVABLE = "unresolvable"
    MISSING_THREAD = "missing_thread"
    # A marker cleared before it could fire, because real work took the
    # thread's idle state over.
    SUPERSEDED_BY_TURN = "superseded_by_turn"
    # Boot recovery of markers left behind by a restart.
    WARM_WINDOW_PASSED = "warm_window_passed"
    INELIGIBLE = "ineligible"
    # The fold's own target connection was account-limited when it fired.
    TARGET_LIMITED = "target_limited"
    # `_create_running` lost the race to a non-idle conflict.
    CONCURRENT_CHECKPOINT = "concurrent_checkpoint"
    BOUNDARY_NOT_DURABLE = "boundary_not_durable"
    BOUNDARY_NOT_ADVANCING = "boundary_not_advancing"
    # No CheckpointRequest reached admission, or the eventual write failed.
    NOT_REQUESTED = "not_requested"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class IdleCheckpointSkip:
    """Why one due marker could not reach admission, short of consuming it."""

    reason: IdleCheckpointReason
    foldable_tokens: int | None = None
    threshold_tokens: int | None = None
    #: The logged cause behind one reason, e.g. a native warm refusal's
    #: no_receipt, base_changed, boundary_mismatch or input_too_large.
    cause: str | None = None


@dataclass(frozen=True, slots=True)
class IdleCheckpointAdmissionResult:
    """Typed result of marker consumption and optional checkpoint creation.

    `disposition` drives control flow; `reason` is the queryable cause and
    is always set. Where the disposition is the whole cause (STALE,
    COALESCED) `reason` restates it from the shared vocabulary; otherwise
    `reason` refines it.
    """

    disposition: IdleCheckpointDisposition
    record: CheckpointRecord | None = None
    reason: IdleCheckpointReason = field(kw_only=True)
