"""Pure reducers shared by the compact Activity projection."""

from __future__ import annotations

from dataclasses import dataclass

from plans import (
    admission_projection,
    node_states,
    not_moving,
    operator_display_state,
    operator_node_state,
    operator_stopped,
    projections,
    service,
    wait_causes,
)
from plans.models import PlanState, Progress


@dataclass(frozen=True)
class ReducedNode:
    condition: str
    review_kind: str | None
    bucket: str
    label_key: str
    tone_key: str
    child_active: bool
    child_working: bool
    executor_stalled: bool
    quiet_block: bool
    liveness_quiet: bool
    requires_user_action: bool
    user_blocker: bool
    user_pause: bool
    wait_deadline: str | None
    wait_deadline_latest: str | None


def _execution_facts(
    row: dict, plan: dict, *, decision_wait: bool, child_cause: dict | None,
) -> tuple[str, bool, bool, bool, bool]:
    causes = [*admission_projection.intrinsic_cause_facts(row, plan), *row["causes"]]
    if decision_wait:
        causes.append({"kind": "waiting", "reason_key": "decision"})
    if child_cause is not None:
        causes.append(child_cause)
    condition = admission_projection.condition_from_causes(
        str(row["state"]), str(plan["state"]), causes, selected=row["attempt_id"] is not None,
    )
    child_active = not_moving.child_running(causes)
    child_working = wait_causes.working_child({"causes": causes}) is not None
    quiet_block = operator_node_state.display_quiet_from_causes(condition, causes)
    liveness_quiet = operator_node_state.quiet_from_causes(
        condition, causes, row["own_prerequisite_ids"],
    )
    return condition, child_active, child_working, quiet_block, liveness_quiet


def _terminal_node(state: str, review_kind: str | None) -> ReducedNode:
    """Return the only compact facts a terminal lifecycle state can expose."""
    display = operator_display_state.TERMINAL_DISPLAY[state]
    return ReducedNode(
        condition="terminal",
        review_kind=review_kind,
        bucket=operator_node_state.leaf_bucket_from_facts(
            state, "terminal", review_kind, display_label=display["label_key"],
        ),
        label_key=display["label_key"],
        tone_key=display["tone_key"],
        child_active=False,
        child_working=False,
        executor_stalled=False,
        quiet_block=False,
        liveness_quiet=False,
        requires_user_action=False,
        user_blocker=False,
        user_pause=False,
        wait_deadline=None,
        wait_deadline_latest=None,
    )


def reduce_node(
    row: dict, plan: dict, *, review_kind: str | None, decision_wait: bool,
    child_cause: dict | None, executor_stop_fact: dict | None,
) -> ReducedNode:
    """Reduce compact canonical facts into one node's Activity state."""
    state = str(row["state"])
    if state in service._TERMINAL_STATES:
        # Terminal lifecycle state has higher precedence than every live fact.
        return _terminal_node(state, review_kind)
    condition, child_active, child_working, quiet_block, liveness_quiet = _execution_facts(
        row, plan, decision_wait=decision_wait, child_cause=child_cause,
    )
    paused = operator_display_state.plan_defers_asks(plan["state"])
    needs_user = operator_node_state.needs_user_from_facts(
        str(row["state"]),
        condition,
        None if row["assignee"] is None else str(row["assignee"]),
        review_kind,
        has_attention=bool(row["has_attention"]),
        plan_paused=paused,
    )
    stopped_reason = operator_stopped.reason_from_facts(
        executor_stop_fact,
        plan_id=str(plan["id"]),
        node_id=str(row["id"]),
        attempt_id=None if row["attempt_id"] is None else str(row["attempt_id"]),
        condition=condition,
    )
    display = operator_node_state.display_from_facts(
        str(row["state"]),
        condition,
        review_kind,
        needs_user=needs_user,
        child_working=child_working,
        executor_stopped=stopped_reason is not None,
        plan_paused=paused,
    )
    shown = operator_node_state.shown_condition_from_facts(
        condition, label=display["label_key"], child_working=child_working,
        quiet_block=quiet_block, unactionable_user_block=bool(row["has_user_block"]) and not needs_user,
    )
    bucket = operator_node_state.leaf_bucket_from_facts(
        str(row["state"]),
        shown,
        review_kind,
        display_label=display["label_key"],
        child_working=child_working,
        quiet_block=quiet_block,
        unactionable_user_block=bool(row["has_user_block"]) and not needs_user,
        plan_paused=paused,
    )
    return ReducedNode(
        condition=condition,
        review_kind=review_kind,
        bucket=bucket,
        label_key=display["label_key"],
        tone_key=display["tone_key"],
        child_active=child_active,
        child_working=child_working,
        executor_stalled=executor_stop_fact is not None and "stalled" in executor_stop_fact,
        quiet_block=quiet_block,
        liveness_quiet=liveness_quiet,
        requires_user_action=needs_user,
        user_blocker=bool(row["has_user_block"]),
        user_pause=bool(row["has_user_pause"]),
        wait_deadline=row["wait_deadline"],
        wait_deadline_latest=row["wait_deadline_latest"],
    )


def plan_state(
    plan: dict, rows: list[dict], reduced: dict[str, ReducedNode],
) -> PlanState:
    """Reduce compact node states into canonical plan progress and motion."""
    counted = node_states.in_play(rows)
    leaves = node_states.leaves(rows)
    incomplete = [
        reduced[str(row["id"])]
        for row in leaves
        if row["state"] not in service._TERMINAL_STATES
    ]
    conditions = [item.condition for item in incomplete]
    moving = any(
        item.condition == "running" and not item.executor_stalled
        for item in incomplete
    ) or any(item.child_working for item in incomplete)
    held = any(item.condition == "blocked" and not item.liveness_quiet for item in incomplete)
    return PlanState(
        progress=Progress(
            done=sum(row["state"] in service._ACCEPTED_STATES for row in counted),
            total=len(counted),
            open=sum(row["state"] not in service._TERMINAL_STATES for row in counted),
            liveness=projections.progress_liveness(
                plan, conditions, moving=moving, held=held,
            ),
        ),
        moving=moving,
    )


def priority_at(
    plan: dict,
    display: dict,
    rows: list[dict],
    reduced: dict[str, ReducedNode],
) -> str:
    """Return when the concrete user ask last changed, else plan recency."""
    if display["label_key"] != "needs_you":
        return str(plan["updated_at"])
    values = [
        str(row["updated_at"])
        for row in rows
        if reduced[str(row["id"])].requires_user_action
    ]
    return max(values, default=str(plan["updated_at"]))
