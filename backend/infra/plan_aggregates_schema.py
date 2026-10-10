"""Stored subtree aggregates of structured-plan nodes (#731).

One definition of the columns, indexes and the narrowed revision trigger, shared
by the fresh-install schema (`infra.plan_schema`) and the 103 -> 104 migration, so
the two cannot diverge. `plans.aggregates` is the only writer of these columns
(`sent_back` aside, which `plans.service.touch_node` sets with the transition).

Names follow the design (shared/planning/plans-reads-scale-with-open-work):

``sent_back``       a sent-back round, as a row fact (was: a `review_rejected` event).
``own_last_changed`` the node's latest event time, else its `updated_at`.
``listed``          the row is not archived (the pane lists it); counts top-level roots.
``child_in_work``   direct children that are not retired.
``own_in_work``     the node itself is not retired (the baseline for ``child_in_work``).
``sub_*``           sums over descendants of what each passes up: ``open`` (fold
                    vector) and ``done``/``failed``/``closed``/``in_work`` (work vector).
``prop_*``          the value this node last passed up; the baseline a change diffs against.
``folded``          settled-or-container with ``sub_open = 0`` (`node_states.folded_ids`).
``folded_at``       when ``folded`` last became 1.
``fold_bucket``     the tone the row shows once folded: done, failed, closed.
``child_folded_*``  folded direct children at this node's level, by tone.

`plans` carries the same counters for its top level as ``top_*``, and ``settled_events``:
the events that moved one of its nodes to accepted, accepted-with-follow-up or failed
(what a liveness wake compares), which a read can no longer count from the event table.
"""

from __future__ import annotations

_COUNT = "INTEGER NOT NULL DEFAULT 0"

#: (name, DDL) of every aggregate column added to ``plan_nodes``.
NODE_AGGREGATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("sent_back", "INTEGER NOT NULL DEFAULT 0 CHECK (sent_back IN (0, 1))"),
    ("own_last_changed", "TEXT"),
    ("listed", "INTEGER NOT NULL DEFAULT 0 CHECK (listed IN (0, 1))"),
    ("child_in_work", _COUNT),
    ("own_in_work", _COUNT),
    ("sub_open", _COUNT),
    ("sub_done", _COUNT),
    ("sub_failed", _COUNT),
    ("sub_closed", _COUNT),
    ("sub_in_work", _COUNT),
    ("prop_open", _COUNT),
    ("prop_done", _COUNT),
    ("prop_failed", _COUNT),
    ("prop_closed", _COUNT),
    ("prop_in_work", _COUNT),
    ("folded", "INTEGER NOT NULL DEFAULT 0 CHECK (folded IN (0, 1))"),
    ("folded_at", "TEXT"),
    ("fold_bucket", "TEXT CHECK (fold_bucket IS NULL OR fold_bucket IN ('done', 'failed', 'closed'))"),
    ("child_folded_done", _COUNT),
    ("child_folded_failed", _COUNT),
    ("child_folded_closed", _COUNT),
)

#: (name, DDL) of every aggregate column added to ``plans``.
PLAN_AGGREGATE_COLUMNS: tuple[tuple[str, str], ...] = tuple(
    (name, _COUNT) for name in (
        "top_open", "top_done", "top_failed", "top_closed", "top_in_work", "top_roots",
        "top_folded_done", "top_folded_failed", "top_folded_closed",
        "settled_events",
    )
)

NODE_AGGREGATE_NAMES = frozenset(name for name, _ in NODE_AGGREGATE_COLUMNS)
PLAN_AGGREGATE_NAMES = frozenset(name for name, _ in PLAN_AGGREGATE_COLUMNS)

#: Every ``plan_nodes`` column that existed before the aggregates. The plan
#: revision trigger fires on an update of these only, so a hook write that
#: touches aggregate columns neither bumps ``plans.revision`` nor moves
#: ``plans.updated_at``. A new ``plan_nodes`` column belongs here unless it is
#: an aggregate (a test pins the partition).
PRE_AGGREGATE_NODE_COLUMNS: tuple[str, ...] = (
    "id", "plan_id", "parent_id", "order_key", "kind", "title", "description",
    "workflow_ref", "workflow_version", "workflow_step", "difficulty", "size",
    "acceptance_criteria", "checkpoint_exception", "state", "assignee",
    "spawn_kind", "owner_thread_id", "executor_thread_id", "spawned_thread_id",
    "follow_up_of", "requirements_revision", "commitment_id",
    "watchdog_budget_seconds", "revision", "created_at", "updated_at", "archived_at",
)

NODE_UPDATE_TRIGGER_SQL = f"""
    CREATE TRIGGER IF NOT EXISTS bump_plan_revision_node_update
    AFTER UPDATE OF {", ".join(PRE_AGGREGATE_NODE_COLUMNS)} ON plan_nodes
    BEGIN
        UPDATE plans SET revision=revision+1, updated_at=NEW.updated_at
        WHERE id=NEW.plan_id;
    END;
"""

#: Open work and finished history are read through these (S2); the third dates a
#: container by its latest unarchived child, and the fourth finds the folded rows that
#: changed in a plan since a cutoff (the recent window of a read) without reading the plan.
#: It is partial on ``folded = 1`` so a read of open rows never prefers it to the open index.
#: The fifth holds only the pending parents whose children have all settled (stale containers,
#: which the fold hides but a verdict or a close over their ancestor still counts as unsettled),
#: and the sixth only the failed rows assigned to the user, by when they last changed, so the
#: chat list finds the failures it asks about without reading the plan's other history.
NODE_AGGREGATE_INDEXES: dict[str, str] = {
    "idx_plan_nodes_open": (
        "CREATE INDEX IF NOT EXISTS idx_plan_nodes_open "
        "ON plan_nodes(plan_id, parent_id, order_key) "
        "WHERE archived_at IS NULL AND folded = 0"
    ),
    "idx_plan_nodes_folded": (
        "CREATE INDEX IF NOT EXISTS idx_plan_nodes_folded "
        "ON plan_nodes(plan_id, parent_id, folded_at DESC, id) "
        "WHERE archived_at IS NULL AND folded = 1"
    ),
    "idx_plan_nodes_last_changed": (
        "CREATE INDEX IF NOT EXISTS idx_plan_nodes_last_changed "
        "ON plan_nodes(parent_id, own_last_changed) WHERE archived_at IS NULL"
    ),
    "idx_plan_nodes_recent": (
        "CREATE INDEX IF NOT EXISTS idx_plan_nodes_recent "
        "ON plan_nodes(plan_id, own_last_changed) WHERE archived_at IS NULL AND folded = 1"
    ),
    "idx_plan_nodes_stale": (
        "CREATE INDEX IF NOT EXISTS idx_plan_nodes_stale ON plan_nodes(plan_id) "
        "WHERE archived_at IS NULL AND folded = 1 AND state = 'pending'"
    ),
    "idx_plan_nodes_user_failed": (
        "CREATE INDEX IF NOT EXISTS idx_plan_nodes_user_failed "
        "ON plan_nodes(plan_id, own_last_changed) "
        "WHERE archived_at IS NULL AND state = 'failed' AND assignee = 'user'"
    ),
}

NODE_AGGREGATE_INDEXES_SQL = ";\n    ".join(NODE_AGGREGATE_INDEXES.values()) + ";"

#: Every plan write asks, under the writer lock, which plans it touched
#: (``plans.status_context.touched_plans``): the node, event and attempt rows written since the
#: transaction began, across the whole instance. These let that ask read the recent rows, so a
#: write costs what it wrote and not the settled history of every plan (#149 S2w).
TOUCH_INDEXES: dict[str, str] = {
    "idx_plan_nodes_updated": (
        "CREATE INDEX IF NOT EXISTS idx_plan_nodes_updated ON plan_nodes(updated_at)"
    ),
    "idx_plan_node_events_created": (
        "CREATE INDEX IF NOT EXISTS idx_plan_node_events_created ON plan_node_events(created_at)"
    ),
    "idx_plan_node_attempts_selected": (
        "CREATE INDEX IF NOT EXISTS idx_plan_node_attempts_selected "
        "ON plan_node_attempts(selected_at)"
    ),
    "idx_plan_node_attempts_released": (
        "CREATE INDEX IF NOT EXISTS idx_plan_node_attempts_released "
        "ON plan_node_attempts(released_at) WHERE released_at IS NOT NULL"
    ),
}

TOUCH_INDEXES_SQL = ";\n    ".join(TOUCH_INDEXES.values()) + ";"


def column_ddl(columns: tuple[tuple[str, str], ...]) -> str:
    """The column definitions as lines of a ``CREATE TABLE`` body."""
    return "".join(f",\n        {name} {ddl}" for name, ddl in columns)
