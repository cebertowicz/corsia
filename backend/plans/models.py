"""Immutable projections returned by the structured-plan service."""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Plan:
    """One durable plan owned by a thread or concrete scope."""

    id: str
    owner_kind: str
    owner_id: str
    parent_plan_id: str | None
    state: str
    revision: int


@dataclass(frozen=True)
class PlanNode:
    """One plan-tree node with its derived dependency status."""

    id: str
    plan_id: str
    parent_id: str | None
    order_key: int
    kind: str
    title: str
    description: str | None
    intent: str | None
    intent_node_id: str | None
    workflow_ref: str | None
    workflow_version: str | None
    workflow_step: str | None
    difficulty: str | None
    size: str | None
    acceptance_criteria: str
    checkpoint_exception: bool
    state: str
    #: Ids of prerequisites not yet accepted, and who placed a block on the
    #: node; both empty unless it is open (`plans.node_waits`).
    unmet_dependencies: list[str]
    held_by: str | None
    assignee: str | None
    spawn_kind: str | None
    owner_thread_id: str | None
    executor_thread_id: str | None
    spawned_thread_id: str | None
    follow_up_of: str | None
    watchdog_budget_seconds: int | None
    requirements_revision: int
    commitment_id: str
    revision: int

    def as_dict(self) -> dict:
        """The node as ``dataclasses.asdict`` returns it, without its recursive walk.

        Every field is a scalar except ``unmet_dependencies``, so one list copy
        keeps the result independent of the node. A mutable field added later
        must be copied here; ``test_plan_node_as_dict_matches_asdict`` fails
        until it is.
        """
        values = {name: getattr(self, name) for name in _NODE_FIELDS}
        values["unmet_dependencies"] = list(self.unmet_dependencies)
        return values


_NODE_FIELDS = tuple(field.name for field in fields(PlanNode))


@dataclass(frozen=True)
class Progress:
    """Liveness-aware, non-authoritative projection for one plan."""

    done: int
    total: int
    liveness: str
    open: int


@dataclass(frozen=True)
class PlanState:
    """One plan's progress plus whether work in it, or delegated below it, runs.

    ``moving`` answers a different question from ``progress.liveness``: liveness
    ranks review above motion for attention, so a plan can read ``needs-review``
    while one of its steps, or a plan delegated below it, is still running.
    """

    progress: Progress
    moving: bool


@dataclass(frozen=True)
class PlanLivenessSignal:
    """Actionable structured-plan leaf count plus stable progress fingerprint."""

    count: int
    signal_hash: str
    plan_id: str | None
    node_ids: tuple[str, ...]
    settled_events: int = 0
