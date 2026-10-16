"""Canonical execution, review, evidence, and cost state for operator nodes."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from plans import admission, node_states, operator_stopped, service, wait_causes
from plans.admission_projection import executor_of
from plans import operator_node_facts as facts
from plans.operator_display_state import (
    NODE_LABEL_KEYS, PLAN_LABEL_KEYS,
    TERMINAL_DISPLAY,
    TONE_KEYS,
    _HUMAN_REVIEWS,
    ask_deferred,
    display_from_facts,
    needs_user_from_facts,
    plan_paused,
    retired_display,
)
#: Leaves whose primary condition opens their path by default. Paused work
#: (a whole-plan pause or a node pause hold) is "Collapsed by default" in the
#: PLANS-CANONICAL-STATE-MATRIX display matrix; review outranks pause, so a
#: review raised before the pause stays current and answerable.
CURRENT_CONDITIONS = frozenset({"running", "waiting", "blocked", "review"})
ATTEMPT_HISTORY_LIMIT = 20
_EMPTY_ATTEMPTS = {"attempt_count": 0, "friction_count": 0, "revision_round": 0}


def _every_cause(execution: dict, test) -> bool:
    causes = execution["causes"]
    return execution["condition"] == "blocked" and bool(causes) and all(map(test, causes))


def _open_prerequisite(cause: dict, open_node_ids) -> bool:
    return (
        cause.get("reason_key") == "dependency"
        and cause.get("dependency_node_id") in open_node_ids
    )


def quiet(execution: dict, open_node_ids) -> bool:
    """Return whether a blocked execution asks nothing of the user.

    Red is for failed work and what the user must clear (owner, 2026-09-16 and
    -24). Live prerequisites in its plan and causes that wait on their own (a
    watch, which is also how a blocker anyone but the user clears is kept, a
    decision, a working child) read as a neutral wait on the row, status read
    and liveness, alone or mixed. A failed prerequisite, a blocker hold (only
    ever the user's) and any derived blocking cause stay blocked. Admission
    stays ``blocked``; wake and selection never read this.
    """
    return quiet_from_causes(execution["condition"], execution["causes"], open_node_ids)


def quiet_from_causes(condition: str, causes: list[dict], open_node_ids) -> bool:
    """Own-plan prerequisite quietness for liveness, distinct from display."""
    return condition == "blocked" and bool(causes) and all(
        cause.get("kind") == "waiting" or (
            not cause.get("placed", False) and _open_prerequisite(cause, open_node_ids)
        ) for cause in causes
    )


def after(execution: dict, titles: Mapping[str, str]) -> str | None:
    """Name the prerequisites a node is only sequenced behind, or ``None``."""
    if not _every_cause(execution, lambda cause: _open_prerequisite(cause, titles)):
        return None
    causes = execution["causes"]
    first = titles[causes[0]["dependency_node_id"]]
    rest = len(causes) - 1
    return f"After {first}" + (f" and {rest} more" if rest else "")


def is_quiet(node: dict) -> bool:
    """Return whether a presented blocker waits on a live cause of its own.

    Visible blockers all use the four-state ``waiting`` label. The internal
    distribution must still distinguish a live prerequisite or watch from a
    failed prerequisite the agent has to re-plan, so it reads the causes rather
    than inferring semantics back from that shared label.
    """
    execution = node["execution"]
    return display_quiet_from_causes(execution["condition"], execution["causes"])


def display_quiet_from_causes(condition: str, causes: list[dict]) -> bool:
    """Live prerequisites read as waits regardless of the displayed scope."""
    return condition == "blocked" and bool(causes) and all(
        cause.get("kind") == "waiting" or (
            not cause.get("placed", False) and cause.get("reason_key") == "dependency"
        ) for cause in causes
    )


def unactionable_user_hold(node: dict) -> bool:
    """Whether a legacy user blocker has no decision, document, or user task.

    New agent calls cannot create this shape. Keeping the compatibility
    predicate here gives every display reader one answer while the agent's
    idle ``next`` listing tells it to raise an ask or release the old hold.
    """
    return not node.get("requires_user_action") and any(
        cause.get("placed") and cause.get("kind") == "blocked"
        and cause.get("owner_kind") == "user"
        for cause in node["execution"]["causes"]
    )


def shown_condition(node: dict) -> str:
    """Return the shown condition, including delegated work shown as Working (#2373)."""
    label, condition = node["display"]["label_key"], node["execution"]["condition"]
    return shown_condition_from_facts(
        condition, label=label,
        child_working=(label == "working" and condition != "running" and _child_plan_working(node["execution"])),
        quiet_block=is_quiet(node), unactionable_user_block=unactionable_user_hold(node),
    )


def shown_condition_from_facts(
    condition: str, *, label: str, child_working: bool, quiet_block: bool,
    unactionable_user_block: bool,
) -> str:
    """One shown-condition adapter for full nodes and narrow facts."""
    if label == "working" and (condition == "running" or child_working):
        return "running"
    if label == "waiting":
        if condition == "running":
            # A provider-reset deadline is displayed as a wait while the
            # selected Turn remains durable. Every roll-up follows the shown
            # condition instead of treating it as ready work.
            return "waiting"
        if condition == "blocked" and (quiet_block or unactionable_user_block):
            return "waiting"
    return condition


#: Conditions whose first cause is the specific thing a row names.
_REASONED_CONDITIONS = frozenset({"waiting", "blocked", "paused"})


def _status_reason(
    execution: dict, sequenced_after: str | None, stopped: str | None,
) -> str | None:
    """The one specific cause a row names under its title, if it has one."""
    if stopped is not None:
        return stopped
    if sequenced_after is not None:
        return sequenced_after
    if execution["condition"] not in _REASONED_CONDITIONS or _child_plan_working(execution):
        return None
    # A whole-plan pause is named once in the plan header, not on every row.
    return next(
        (
            cause.get("reason")
            for cause in execution["causes"]
            if cause.get("reason_key") != "plan_paused" and cause.get("reason")
        ),
        None,
    )



def _needs_user(node: dict, review: dict | None, execution: dict) -> bool:
    """Whether this open row has a concrete action the user can take now."""
    return needs_user_from_facts(
        str(node["state"]),
        str(execution["condition"]),
        None if node.get("assignee") is None else str(node["assignee"]),
        None if review is None else str(review["kind"]),
        has_attention=any(
            link["role"] == "attention" and link.get("attention_state") == "raised"
            for link in node.get("artifact_links", [])
        ),
        plan_paused=plan_paused(execution),
    )


def _display(node: dict, execution: dict, review: dict | None, *, stopped: str | None = None) -> dict[str, str]:
    return display_from_facts(
        str(node["state"]),
        str(execution["condition"]),
        None if review is None else str(review["kind"]),
        needs_user=_needs_user(node, review, execution),
        child_working=_child_plan_working(execution),
        # A stopped selected executor makes an otherwise-running row wait.
        executor_stopped=stopped is not None,
        worker_needs_input=any(
            cause.get("reason_key") == "worker_needs_input"
            for cause in execution["causes"]
        ),
        plan_paused=plan_paused(execution),
    )


def _sent_back(node: dict) -> str | None:
    """Why an open revision is being redone: the feedback its round was sent back with."""
    feedback = node.get("sent_back_because")
    return f"Sent back: {feedback}" if feedback else None


def _child_plan_working(execution: dict) -> bool:
    """Return whether a delegated child is doing this step's work right now."""
    return wait_causes.child_is_working(execution)


def _executor(executor_id: str | None, node, labels: dict | None) -> dict:
    """Return the executor identity and its human label for one node."""
    if executor_id is not None:
        label = None if labels is None else labels.get(executor_id, "Unavailable executor")
    else:
        label = "Unassigned" if node["kind"] == "task" else "Not applicable"
    return {"thread_id": executor_id, "label": label}


def project(
    projected,
    review_targets: dict[str, dict],
    *,
    db,
    now: datetime | None = None,
    costs: bool = True,
    memo: dict | None = None,
    lean: bool = False,
    detail_ids: frozenset[str] | None = None,
    titles: Mapping[str, str] | None = None,
) -> dict[str, dict]:
    """Project every node from canonical owners without writing or client inference.

    ``memo`` is the rollup's per-request plan-state memo, so a delegated step's
    child plan the rollup already walked is not walked again here.

    ``now`` is the one clock for every time-sensitive fact (status paging keeps
    one across a continuation). ``costs=False`` leaves both cost fields None for
    the status journal, which runs under the writer lock and shows none (#2489).

    ``detail_ids`` (open-work read, ``plans.open_rollup``; ``titles`` names the other rows): facts are read for the
    listed rows alone; only these ids get rolled, commitment and initiative costs.

    ``lean`` is for a reader that takes only the plan header's and the ask
    list's facts (``execution``, ``display``, ``requires_user_action``). It
    skips the reads behind the rest: ``executor.label``, ``evidence`` and
    ``last_transition_at`` are None, a settled node's recorded reason is not in
    ``status_reason``, and ``execution`` has no attempts and zero counts.
    """
    node_plans = [(node, item.plan) for item in projected for node in item.nodes]
    executor_by_node = {node["id"]: executor_of(node, plan) for node, plan in node_plans}
    labels, history, attempt_counts, evidence, changed, settled_because = (
        facts.NO_DETAIL if lean else facts.detail(
            projected, node_plans, executor_by_node, limit=ATTEMPT_HISTORY_LIMIT, db=db,
            scoped=detail_ids is not None,
        )
    )
    if titles is None:
        titles = {node["id"]: node["title"] for node, _plan in node_plans}
    executions = admission.project_many(node_plans, db=db, memo=memo)
    stops = operator_stopped.nodes(node_plans, executor_by_node, executions, db=db)
    commitment_costs, node_costs, initiatives = facts.project_costs(
        node_plans, db=db, now=now or datetime.now(timezone.utc), wanted=costs,
        detail_ids=detail_ids,
    )
    states: dict[str, dict] = {}

    for item in projected:
        for node in item.nodes:
            execution = executions[node["id"]]
            executor_id = executor_by_node[node["id"]]
            execution["attempts"] = history.get(node["id"], [])
            execution.update(attempt_counts.get(node["id"], _EMPTY_ATTEMPTS))
            review = review_targets.get(node["id"])
            commitment_id = str(node["commitment_id"])
            is_commitment_root = node["id"] == commitment_id
            sequenced_after = after(execution, titles)
            stopped = operator_stopped.at(
                stops.get(executor_id), node, item.plan, execution,
            )
            states[node["id"]] = {
                "executor": _executor(executor_id, node, labels),
                "execution": execution,
                "display": _display(node, execution, review, stopped=stopped),
                "status_reason": settled_because.get(node["id"]) or _status_reason(
                    execution, sequenced_after, stopped) or _sent_back(node),
                "evidence": evidence.get(node["id"]),
                # The commitment total is true only at its one root; every
                # other node in the tree would otherwise repeat the whole
                # commitment's figure on its own row.
                "commitment_cost": commitment_costs.get(commitment_id) if is_commitment_root else None,
                "node_cost": node_costs[str(item.plan["id"])][node["id"]],
                "initiative": initiatives.get(node["id"]),
                "last_transition_at": None if lean else changed[node["id"]],
                "requires_user_action": _needs_user(node, review, execution),
            }
    return states


def next_node_ids(nodes: list[dict]) -> list[str]:
    """Choose the first conflict-free ready leaf for each free executor.

    A delegated step already shown in progress is skipped: its child is doing
    that work, so it is not the executor's next task.
    """

    parent_ids = node_states.parent_ids(nodes)
    chosen: list[str] = []
    executors: set[str] = set()
    for node in nodes:
        execution = node["execution"]
        executor = node["executor"]["thread_id"]
        if (
            node["kind"] != "task"
            or node["id"] in parent_ids
            or execution["condition"] != "ready"
            or _child_plan_working(execution)
            or execution["selection"] is not None
            or execution["conflict"] is not None
            or executor is None
            or executor in executors
        ):
            continue
        chosen.append(node["id"])
        executors.add(executor)
    return chosen


#: The buckets one leaf is counted in, for a branch and for the whole plan.
DISTRIBUTION_KEYS = (
    "done", "active", "pending_ready", "review_parent_agent", "review_human",
    "waiting", "blocked", "paused", "failed",
)
OPEN_KEYS = (
    "active", "pending_ready", "review_parent_agent", "review_human",
    "waiting", "blocked", "paused",
)


def leaf_bucket_from_facts(
    state: str,
    condition: str,
    review_kind: str | None = None,
    *,
    display_label: str,
    child_working: bool = False,
    quiet_block: bool = False,
    unactionable_user_block: bool = False,
    plan_paused: bool = False,
) -> str:
    """Reduce compact lifecycle facts into the canonical distribution bucket."""
    if state in service._ACCEPTED_STATES:
        return "done"
    if state == "failed":
        return state
    if ask_deferred(condition, review_kind, plan_paused=plan_paused):
        return "paused"
    if condition == "review":
        return "review_human" if review_kind in _HUMAN_REVIEWS else "review_parent_agent"
    if display_label == "working":
        return "active" if condition == "running" or child_working else "pending_ready"
    if condition == "waiting" or quiet_block:
        return "waiting"
    if condition == "blocked":
        return "waiting" if unactionable_user_block else "blocked"
    if condition == "paused":
        return "paused"
    return "pending_ready"


def leaf_bucket(node: dict) -> str:
    """Name the one bucket a leaf counts in, by its canonical lifecycle/condition."""
    review, label = node["review"] or {}, str(node["display"]["label_key"])
    return leaf_bucket_from_facts(
        str(node["state"]),
        shown_condition(node),
        review.get("kind"),
        display_label=label,
        child_working=label == "working" and _child_plan_working(node["execution"]),
        quiet_block=is_quiet(node),
        unactionable_user_block=unactionable_user_hold(node),
        plan_paused=plan_paused(node["execution"]),
    )


def leading(counts: dict[str, int]) -> str:
    """The internal precedence for reading a container's leaf distribution:
    review, then motion before a block, then all-waiting/all-paused, then
    ``open``; with nothing open, ``failed``, ``done``, else ``empty``. The plan
    projection evaluates ``not_moving`` separately for agent continuation."""
    for key in ("review_human", "review_parent_agent", "active", "blocked"):
        if counts[key]:
            return key
    incomplete = sum(counts[key] for key in OPEN_KEYS)
    if incomplete:
        for key in ("waiting", "paused"):
            if counts[key] == incomplete:
                return key
        return "open"
    for key in ("failed", "done"):
        if counts[key]:
            return key
    return "empty"
