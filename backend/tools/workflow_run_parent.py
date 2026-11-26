"""Where an agent's workflow run may sit: under the plain phase that holds its piece of work.

A run (a phase with ``workflow_ref``) at the top level splits one piece of work into
several top-level rows, so the agent tool path refuses an add, insert or edit that
would put one there. Plain phases and tasks keep the top level. The checks read nodes
the caller named only on a plan it may reach, so their answer never says what another
plan holds; anything else is left to the mutation's own error.
"""

from __future__ import annotations

from typing import Any

from plans.public import PlanInputError, authorize_thread_owner

RUN_PARENT_REFUSAL = (
    "A workflow run starts under the plain phase (no workflow_ref) that holds its piece of "
    "work: set parent_id to that phase, or add the phase first if the work has none."
)


def validate(operation: str, params: dict[str, Any], db, plan_id: str, actor) -> None:
    """Refuse an add, insert or edit that would leave a run at the top level."""

    if operation in {"add", "insert"}:
        validate_run_parent(params, db, plan_id, actor)
    elif operation == "edit":
        validate_run_move(params, db, plan_id, actor)


def _reachable(db, plan_id: str, actor) -> bool:
    """Whether ``actor`` may reach this plan under the one access rule (``authorize_thread_owner``)."""

    plan = db.execute("SELECT owner_kind, owner_id FROM plans WHERE id=?", (plan_id,)).fetchone()
    if plan is None or plan["owner_kind"] != "thread":
        return False
    try:
        authorize_thread_owner(db, plan["owner_id"], actor)
    except PermissionError:
        return False
    return True


def validate_run_parent(params: dict[str, Any], db, plan_id: str, actor) -> None:
    """Refuse a new run that would land at the top level.

    An insert takes its parent from its ``before_node_id`` sibling, so only a parent that
    resolves to none is refused. A sibling that can't be read is left to the mutation's own
    error.
    """

    if params.get("kind") != "phase" or not isinstance(params.get("workflow_ref"), str):
        return
    if params.get("parent_id") is not None:
        return
    before = params.get("before_node_id")
    if before is not None:
        if not _reachable(db, plan_id, actor):
            return
        sibling = db.execute(
            "SELECT parent_id FROM plan_nodes WHERE id=? AND plan_id=?", (before, plan_id),
        ).fetchone()
        if sibling is None or sibling["parent_id"] is not None:
            return
    raise PlanInputError(RUN_PARENT_REFUSAL)


def validate_run_move(params: dict[str, Any], db, plan_id: str, actor) -> None:
    """Refuse an edit that would move a run from under its phase to the top level.

    An edit with ``parent_id`` null names the top level. A run already there is not moved,
    so it is left alone.
    """

    if "parent_id" not in params or params["parent_id"] is not None:
        return
    ids = params.get("node_ids") or [params.get("node_id")]
    ids = [node_id for node_id in ids if isinstance(node_id, str)]
    if not ids or not _reachable(db, plan_id, actor):
        return
    marks = ",".join("?" for _ in ids)
    lifted = db.execute(
        f"SELECT 1 FROM plan_nodes WHERE plan_id=? AND id IN ({marks}) AND kind='phase' "
        "AND workflow_ref IS NOT NULL AND parent_id IS NOT NULL LIMIT 1",
        (plan_id, *ids),
    ).fetchone()
    if lifted is not None:
        raise PlanInputError(RUN_PARENT_REFUSAL)
