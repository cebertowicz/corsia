"""Transactional domain service for the P1 structured-plan model."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from infra.database import get_db
import plans.errors as errors
import plans.node_waits as node_waits
from plans import aggregates, plan_write, tree_rows, writer_context
from plans.models import Plan, PlanNode
# Re-exported: the lifecycle table lives in `plans.node_states`.
from plans.node_states import (  # noqa: F401
    CLOSED, _ACCEPTED_STATES, _TERMINAL_STATES, _TRANSITIONS, leaves,
)
_OWNER_KINDS = frozenset({"thread", "project_scope", "workspace_scope"})
NODE_KINDS = frozenset({"phase", "track", "task"})
ASSIGNEES = frozenset({"user", "coordinator", "subagent"})

#: Re-exported from `plans.errors`, which owns the taxonomy and imports nothing
#: from `plans`, so raising cannot create a cycle.
PlanError, PlanInputError, PlanRefusal = errors.PlanError, errors.PlanInputError, errors.PlanRefusal

@contextmanager
def _connection(db=None) -> Iterator[sqlite3.Connection]:
    """Yield a caller transaction or a service-owned database connection."""
    connection = db or get_db()
    try:
        yield connection
    finally:
        if db is None:
            connection.close()

@contextmanager
def _write(connection: sqlite3.Connection) -> Iterator[None]:
    """Make one plan write atomic through the canonical transaction seam."""
    with plan_write.transaction(connection):
        yield

def _now() -> str:
    """Return the canonical timestamp for plan persistence."""
    return datetime.now(timezone.utc).isoformat()

#: Columns a caller may set with the revision bump (named by code, never input).
_TOUCH_COLUMNS = frozenset({
    "state", "order_key", "title", "description", "acceptance_criteria",
    "watchdog_budget_seconds", "requirements_revision", "archived_at", "size",
    "commitment_id", "spawned_thread_id", "spawn_kind", "sent_back",
})

def touch_node(connection: sqlite3.Connection, node_id: str, *, at: str | None = None,
               **columns) -> int:
    """Advance one node's revision: the only writer of ``plan_nodes.revision``.

    It moves exactly when the node itself changed; a sibling's order shift is
    not that node's change. Returns the number of rows written.
    """
    unknown = set(columns) - _TOUCH_COLUMNS
    if unknown:
        raise ValueError(f"touch_node cannot set {sorted(unknown)}")
    assignments = "".join(f"{name}=?, " for name in columns)
    stamp = at or _now()
    written = writer_context.execute(
        connection, f"UPDATE plan_nodes SET {assignments}revision=revision+1, updated_at=? WHERE id=?",
        (*columns.values(), stamp, node_id),
    ).rowcount
    if written and aggregates.INPUT_COLUMNS & columns.keys():
        aggregates.apply(connection, node_id, at=stamp)
    return written

def _id() -> str:
    """Create a collision-resistant plan record identity."""
    return uuid.uuid4().hex

def _required_text(value: str, field: str) -> str:
    """Normalize a required textual field or raise a domain error."""
    normalized = (value or "").strip()
    if not normalized:
        raise PlanInputError(f"{field} is required")
    return normalized

#: Longest node title the plan pane can show without truncating it. The row is
#: one line with an ellipsis, so a longer title hides its own tail.
MAX_NODE_TITLE = 60

def validate_node_title(value: str) -> str:
    """Normalize a node title and refuse one the plan pane cannot show.

    Raises:
        PlanInputError: The title is empty or longer than ``MAX_NODE_TITLE``.
    """
    normalized = _required_text(value, "node title")
    if len(normalized) > MAX_NODE_TITLE:
        raise PlanInputError(
            f"node title is {len(normalized)} characters, over the "
            f"{MAX_NODE_TITLE}-character limit the plan row can show. Name the "
            "outcome in 1-3 words and move detail to the description."
        )
    return normalized

def _validate_dod(criteria: str, checkpoint_exception: bool) -> str:
    """Require a verifiable DOD, including the explicit checkpoint contract."""
    normalized = _required_text(criteria, "acceptance criteria")
    if checkpoint_exception:
        lowered = normalized.lower()
        if "plan" not in lowered or "expan" not in lowered:
            raise PlanInputError("checkpoint DOD must name the resulting plan expansion")
    return normalized

def _plan_from_row(row: sqlite3.Row) -> Plan:
    """Convert one plans row into the public immutable projection."""
    return Plan(row["id"], row["owner_kind"], row["owner_id"], row["parent_plan_id"],
                row["state"], row["revision"])

def _node_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    waits: tuple[list[str], str | None] | None = None,
) -> PlanNode:
    """Convert one node row, optionally reusing a batched `node_waits` projection."""
    if waits is None:
        waits = node_waits.project(connection, [row])[str(row["id"])]
    workflow = {
        name: row[name] if name in row.keys() else None
        for name in ("workflow_ref", "workflow_version", "workflow_step", "difficulty", "size")
    }
    return PlanNode(
        id=row["id"], plan_id=row["plan_id"], parent_id=row["parent_id"],
        order_key=row["order_key"], kind=row["kind"], title=row["title"],
        description=row["description"], workflow_ref=workflow["workflow_ref"],
        workflow_version=workflow["workflow_version"], workflow_step=workflow["workflow_step"],
        difficulty=workflow["difficulty"], size=workflow["size"],
        acceptance_criteria=row["acceptance_criteria"],
        checkpoint_exception=bool(row["checkpoint_exception"]), state=row["state"],
        unmet_dependencies=waits[0], held_by=waits[1],
        assignee=row["assignee"], spawn_kind=row["spawn_kind"],
        owner_thread_id=row["owner_thread_id"],
        executor_thread_id=row["executor_thread_id"],
        spawned_thread_id=row["spawned_thread_id"], follow_up_of=row["follow_up_of"],
        watchdog_budget_seconds=row["watchdog_budget_seconds"],
        requirements_revision=row["requirements_revision"],
        commitment_id=row["commitment_id"], revision=row["revision"],
    )

def _plan_row(connection: sqlite3.Connection, plan_id: str) -> sqlite3.Row:
    """Return one plan or reject an unknown plan identity."""
    row = connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise errors.UnknownPlan(f"unknown plan: {plan_id}")
    return row

def _plan_tree_rows(connection: sqlite3.Connection, plan_id: str) -> list[sqlite3.Row]:
    """Return a plan's unarchived nodes in ``order_key, id`` order."""
    return tree_rows.many(connection, [plan_id])[str(plan_id)]


def _plan_leaf_rows(connection: sqlite3.Connection, plan_id: str) -> list[sqlite3.Row]:
    """Return a plan's unarchived leaves in plan order (``node_states.leaves``)."""
    return leaves(_plan_tree_rows(connection, plan_id))

def _node_row(connection: sqlite3.Connection, node_id: str) -> sqlite3.Row:
    """Return one node or reject an unknown node identity."""
    row = connection.execute("SELECT * FROM plan_nodes WHERE id=?", (node_id,)).fetchone()
    if row is None:
        raise PlanError(f"unknown plan node: {node_id}")
    return row

#: The one refusal identity for mutating an archived node.
ARCHIVED_NODE = "plan node is archived"
CLOSED_NODE = "plan node is closed"

def require_live_node(node: sqlite3.Row) -> None:
    """Refuse any further mutation of an archived or closed node.

    Archive and close each end a node's mutable life: it cannot transition, be
    restated, gain a dependency or watch, or take a child. Close used to
    archive in the same write (#1887), so refusing both keeps every writer as
    it was; the one act still open to a closed node is its explicit archive
    (``archival.archive_node``). Each refusal is a single identity so every
    caller reports the same reason.
    """
    if node["archived_at"] is not None:
        raise PlanError(ARCHIVED_NODE)
    if node["state"] == CLOSED:
        raise PlanError(CLOSED_NODE)

def _live_node_row(connection: sqlite3.Connection, node_id: str) -> sqlite3.Row:
    """Resolve a node that may still be written, or refuse with one identity.

    Every writer that mutates an *existing* node resolves through here, so the
    invariant lives at the primitive rather than in seven owner-specific guards
    that would drift. Readers and the append-only diagnostic writer
    ``record_runner_event`` keep ``_node_row``.
    """
    node = _node_row(connection, node_id)
    require_live_node(node)
    return node

def _event(connection: sqlite3.Connection, node_id: str, event: str, *, actor_id: str | None,
           provenance: str, prior_state: str | None = None, new_state: str | None = None,
           verification_result: str | None = None, payload: dict | None = None,
           causal_event_id: str | None = None) -> str:
    """Append an auditable event inside the surrounding plan transaction."""
    event_id = _id()
    created_at = _now()
    connection.execute(
        "INSERT INTO plan_node_events "
        "(id,node_id,actor_id,provenance,event,prior_state,new_state,verification_result,"
        "causal_event_id,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, node_id, actor_id, _required_text(provenance, "provenance"), event,
         prior_state, new_state, verification_result, causal_event_id,
         json.dumps(payload, sort_keys=True) if payload else None, created_at),
    )
    aggregates.note_event(connection, node_id, new_state)
    from plans.plan_events import project_node_event

    project_node_event(
        connection, node_id=node_id, event=event, event_id=event_id,
        actor_id=actor_id, new_state=new_state, payload=payload,
        provenance=provenance, created_at=created_at)
    return event_id

def record_runner_event(*args, **kwargs) -> bool:
    """Expose append-only runner audit events without coupling source modules."""

    from plans.runner_events import record

    return record(*args, **kwargs)

def ensure_plan(owner_kind: str, owner_id: str, *, db=None) -> Plan:
    """Create or return the single durable plan for one concrete owner."""
    from plans.hierarchy import ensure_plan_for_owner

    return ensure_plan_for_owner(owner_kind, owner_id, db=db)

def _create_node(*args, **kwargs) -> PlanNode:
    """Expose the extracted canonical node-creation writer."""

    from plans.node_creation import create_node

    return create_node(*args, **kwargs)

def add_node(plan_id: str, *, kind: str, title: str, acceptance_criteria: str,
             parent_id: str | None = None, order_key: int | None = None,
             description: str | None = None, workflow_ref: str | None = None,
             workflow_version: str | None = None, workflow_step: str | None = None,
             difficulty: str | None = None, size: str | None = None,
             checkpoint_exception: bool = False,
             assignee: str | None = None, spawn_kind: str | None = None,
             watchdog_budget_seconds: int | None = None,
             actor_id: str | None = None,
             provenance: str = "plan_service", db=None) -> PlanNode:
    """Add one DOD-bearing node at a valid tree position."""
    with plan_write.transaction(db) as connection:
        return _create_node(
            connection, plan_id, kind=kind, title=title,
            acceptance_criteria=acceptance_criteria, parent_id=parent_id,
            order_key=order_key, description=description,
            workflow_ref=workflow_ref, workflow_version=workflow_version,
            workflow_step=workflow_step, difficulty=difficulty, size=size,
            checkpoint_exception=checkpoint_exception, assignee=assignee,
            spawn_kind=spawn_kind,
            watchdog_budget_seconds=watchdog_budget_seconds, follow_up_of=None,
            actor_id=actor_id, provenance=provenance, node_id=_id(),
        )

def get_node(node_id: str, *, db=None) -> PlanNode:
    """Read one node with its current dependency-derived blocked status."""
    with _connection(db) as connection:
        return _node_from_row(connection, _node_row(connection, node_id))

def add_dependency(*args, **kwargs):
    """Expose the extracted dependency writer without an import cycle."""
    from plans.dependencies import add_dependency as add

    return add(*args, **kwargs)

def remove_dependency(*args, **kwargs):
    """Expose the extracted symmetric removal without an import cycle."""
    from plans.dependencies import remove_dependency as remove

    return remove(*args, **kwargs)

# reorder_node is re-exported from plans.mutations below.

def transition_node(node_id: str, new_state: str, *, actor_id: str | None = None,
                    provenance: str = "plan_service", verification_result: str | None = None,
                    verified_by: str | None = None, expected_revision: int | None = None,
                    db=None) -> PlanNode:
    """Apply one legal state transition and append its provenance event."""
    if new_state == "accepted_with_followup":
        raise PlanError("use accept_with_follow_up for atomic linked follow-up creation")
    with plan_write.transaction(db) as connection:
        node = _node_row(connection, node_id)
        if expected_revision is not None:
            errors.require_node_revision(node, expected_revision)
        return _transition(connection, node_id, new_state, actor_id=actor_id,
                           provenance=provenance, verification_result=verification_result,
                           verified_by=verified_by)

def _transition(connection: sqlite3.Connection, node_id: str, new_state: str, *, actor_id: str | None,
                provenance: str, verification_result: str | None, verified_by: str | None,
                settles_worker: bool = True, payload: dict | None = None,
                sent_back: bool = False) -> PlanNode:
    """Apply a validated transition; ``settles_worker=False`` keeps a rejected step's worker.

    ``settles_worker=False`` is also the one sign that the work continues on a
    revision, so dependents in other plans are told only otherwise: the
    rejection moves them to the revision and they keep waiting.

    ``payload`` carries the outcome's own durable fact on the transition event
    -- a close's reason -- so the record of why is the same row as the record
    of what, and no consumer has to join two events to read one outcome.

    ``sent_back`` marks the close as a rejection's round, in the same write as
    the state (the row fact the plan aggregates read).
    """
    from plans import material_events

    node = _live_node_row(connection, node_id)
    old_state = node["state"]
    affected = material_events.transition_affected_nodes(connection, node_id)
    blocked_before = material_events.blocked_snapshot(connection, affected)
    if new_state not in _TRANSITIONS.get(old_state, frozenset()):
        raise errors.illegal_transition(
            old_state, new_state, _TRANSITIONS.get(old_state, frozenset()),
        )
    if new_state in _ACCEPTED_STATES:
        from plans.review_verification import require_acceptance_verification

        require_acceptance_verification(
            connection, node, verification_result=verification_result,
            verified_by=verified_by, actor_id=actor_id, provenance=provenance,
        )
    if new_state == "failed" and "sub_open" in node.keys() and node["sub_open"]:
        raise errors.open_descendants(node["sub_open"])
    marks: dict[str, Any] = {"sent_back": 1} if sent_back else {}
    touch_node(connection, node_id, state=new_state, **marks)
    recorded = {**(payload or {}), **({"verified_by": verified_by} if verified_by else {})}
    event_id = _event(
        connection, node_id, "state_transition", actor_id=actor_id,
        provenance=provenance, prior_state=old_state, new_state=new_state,
        verification_result=verification_result,
        payload=recorded or None,
    )
    if new_state in _TERMINAL_STATES:
        from plans.worker_retirement import settle_terminal

        settle_terminal(
            connection, node_id, new_state, event_id=event_id, actor_id=actor_id,
            provenance=provenance, settles_worker=settles_worker,
        )
    material_events.settle_blocked_deltas(
        connection, blocked_before, actor_id=actor_id, provenance=provenance,
        causal_event_id=event_id,
    )
    if new_state in _TERMINAL_STATES and settles_worker:
        from plans.cross_plan import conductor_task_settled, prerequisite_settled

        prerequisite_settled(connection, node_id, actor_id=actor_id, provenance=provenance)
        conductor_task_settled(connection, node_id, outcome=new_state)
    return _node_from_row(connection, _node_row(connection, node_id))

def add_follow_up(node_id: str, *, title: str, acceptance_criteria: str,
                  size: str | None = None, actor_id: str | None = None,
                  provenance: str = "plan_service", db=None) -> PlanNode:
    """Expose the extracted bounded follow-up writer."""
    from plans.followups import add_follow_up as add

    return add(node_id, title=title, acceptance_criteria=acceptance_criteria, size=size,
               actor_id=actor_id, provenance=provenance, db=db)

def accept_with_follow_up(node_id: str, *, title: str, acceptance_criteria: str,
                          size: str | None = None, actor_id: str | None = None,
                          provenance: str = "plan_service",
                          verification_result: str | None = None, verified_by: str | None = None,
                          expected_revision: int | None = None, db=None) -> tuple[PlanNode, PlanNode]:
    """Expose atomic acceptance with an extracted bounded follow-up writer."""
    from plans.followups import accept_with_follow_up as accept

    return accept(
        node_id, title=title, acceptance_criteria=acceptance_criteria, size=size,
        actor_id=actor_id, provenance=provenance,
        verification_result=verification_result, verified_by=verified_by,
        expected_revision=expected_revision, db=db,
    )

from plans.projections import progress  # noqa: E402

def reorder_node(*args, **kwargs):
    """Lazily expose the extracted reorder mutation without an import cycle."""
    from plans.mutations import reorder_node as reorder

    return reorder(*args, **kwargs)
