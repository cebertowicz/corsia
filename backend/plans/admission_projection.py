"""Batched canonical projection of plan admission and executor occupancy."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import sqlite3

import plans.admission_attempts as admission_attempts
import plans.blocked_wait as blocked_wait
import plans.node_waits as node_waits
import plans.service as service


@dataclass(frozen=True)
class _Facts:
    """Admission facts for a batch of nodes, each kind read in one query."""

    holds: dict[str, list[dict]]
    dependencies: dict[str, list[dict]]
    watches: dict[str, list[dict]]
    attempts: dict[str, sqlite3.Row]
    in_flight: dict[str, int]
    decisions: dict[str, list[dict]]
    children: dict[str, list[dict]]
    paused_since: dict[str, str]


def intrinsic_cause_facts(node, plan) -> list[dict]:
    """Intrinsic lifecycle causes without display prose."""
    causes = []
    if plan["state"] == "paused":
        causes.append({"kind": "paused", "reason_key": "plan_paused"})
    if assignment_missing(node, plan):
        causes.append({"kind": "blocked", "reason_key": "assignment_missing"})
    return causes


def _intrinsic_causes(node, plan, paused_since: dict[str, str]) -> list[dict]:
    facts = intrinsic_cause_facts(node, plan)
    causes = []
    if any(fact["reason_key"] == "plan_paused" for fact in facts):
        causes.append(
            {
                "kind": "paused",
                "reason_key": "plan_paused",
                "reason": "Plan is paused",
                "owner_kind": "thread",
                "owner_id": plan["owner_id"],
                "created_at": paused_since.get(str(plan["id"])),
            }
        )
    if any(fact["reason_key"] == "assignment_missing" for fact in facts):
        causes.append(
            {
                "kind": "blocked",
                "reason_key": "assignment_missing",
                "reason": "No executor is assigned",
                "owner_kind": "system",
                "owner_id": None,
            }
        )
    return causes


def assignment_missing(node, plan) -> bool:
    """Whether a task outside a thread plan has no intrinsic executor."""
    return (
        node["kind"] == "task" and node["executor_thread_id"] is None
        and node["owner_thread_id"] is None and plan["owner_kind"] != "thread"
    )


def _hold(row) -> dict:
    # `placed` marks a hold an actor put there, as opposed to a derived cause.
    return {
        "id": row["id"],
        "placed": True,
        "kind": row["kind"],
        "reason_key": row["reason_key"],
        "reason": row["reason"],
        "owner_kind": row["owner_kind"],
        "owner_id": row["owner_id"],
        "created_at": row["created_at"],
    }


def _indexes(node_ids: list[str], *, db) -> tuple[dict, dict, dict, dict]:
    if not node_ids:
        return {}, {}, {}, {}
    holds = {
        node_id: [_hold(row) for row in rows]
        for node_id, rows in node_waits.open_holds(db, node_ids).items()
    }
    dependencies: dict[str, list[dict]] = defaultdict(list)
    for node_id, rows in node_waits.unmet_prerequisites(db, node_ids).items():
        for row in rows:
            # A prerequisite that failed will never satisfy this dependency, so
            # "waiting" would be the same false calm the plan surface avoids
            # elsewhere. Name it as the distinct cause it is. (A closed one
            # never waits here: a close drops the node's edges.)
            fact = node_waits.dependency_cause(row)
            failed = fact["reason_key"] == "dependency_failed"
            owner = row["foreign_owner"]
            title = f"{row['title']} ({row['foreign_label']}'s plan)" if owner else row["title"]
            dependencies[node_id].append(
                {
                    **fact,
                    "reason": (
                        f"{title} failed and cannot satisfy this dependency"
                        if failed
                        else f"Waiting for {title}"
                    ),
                    "owner_kind": "thread" if owner else "system",
                    "owner_id": owner,
                }
            )

    watches: dict[str, list[dict]] = defaultdict(list)
    for node_id, rows in node_waits.armed_watches(db, node_ids).items():
        for row in rows:
            watches[node_id].append(
                {
                    **node_waits.watch_cause(row),
                    "reason": blocked_wait.watch_reason(row),
                    "owner_kind": row["blocked_owner_kind"] or "external",
                    "owner_id": row["blocked_owner_id"],
                    "created_at": row["armed_at"],
                    # Set aside until ends_at, and why; the row says the date.
                    **(
                        {"scheduled_reason": row["scheduled_reason"]}
                        if row["scheduled_reason"] else {}
                    ),
                }
            )

    attempts = admission_attempts.open_attempts(node_ids, db=db)
    return dict(holds), dict(dependencies), dict(watches), attempts


def executor_of(node, plan) -> str | None:
    """Resolve who runs a node, or None when nobody can.

    Only a plan's owner runs its tasks: a task in a thread's plan is run by that
    thread whatever an older row names. A node in a plan without a thread owner
    keeps its recorded assignment, or has none.
    """
    if plan["owner_kind"] == "thread":
        return str(plan["owner_id"])
    value = node["executor_thread_id"] or node["owner_thread_id"]
    return str(value) if value is not None else None


def superseded_by(attempt, node, plan) -> str | None:
    """Name what superseded an attempt, or return None while it still fits its task.

    An attempt is selected for its task's requirements revision and executor.
    A requirements edit (``'requirements_changed'``) supersedes it, and so does
    an executor that no longer runs the task (``'executor_changed'``): an older
    attempt a child still holds in its parent's plan. ``selection_validity``
    never keeps a superseded attempt as a selection, and the projection never
    shows one as running, so both read this one rule.
    """
    if attempt["requirements_revision"] != node["requirements_revision"]:
        return "requirements_changed"
    if attempt["executor_thread_id"] != executor_of(node, plan):
        return "executor_changed"
    return None


def _facts(node_plans, *, db, memo=None) -> _Facts:
    """Read every admission fact a batch of nodes needs, one query per kind.

    ``memo`` carries the plan states a delegated step's wait reads, so a
    projection that walks related plans walks each of them once.
    """
    from plans import wait_causes

    pairs = list(node_plans)
    holds, dependencies, watches, attempts = _indexes(
        [str(node["id"]) for node, _plan in pairs], db=db,
    )
    in_flight = admission_attempts.effects_in_flight(db, [str(row["id"]) for row in attempts.values()])
    return _Facts(
        holds,
        dependencies,
        watches,
        attempts,
        in_flight,
        wait_causes.decision_causes(pairs, db=db),
        wait_causes.delegation_causes(pairs, db=db, memo=memo),
        wait_causes.paused_since(pairs, db=db),
    )


def _live_executors(executors: set[str], *, db) -> set[str]:
    """Return the executors that have a running or retrying Turn now.

    Read only for the `turn_active` display field. It never decides a
    condition or a conflict, and a Turn's accounting columns take no part.
    """
    if not executors:
        return set()
    marks = ",".join("?" for _ in executors)
    rows = db.execute(
        f"""SELECT DISTINCT thread_id FROM active_turns
            WHERE thread_id IN ({marks})
              AND status='running'""",
        tuple(sorted(executors)),
    ).fetchall()
    return {str(row["thread_id"]) for row in rows}


def _conflict_index(plan_ids: set[str], executors: set[str], *, db) -> dict:
    """Index the task occupying each (plan, executor) slot.

    Read through `admission_attempts.slot_holders`, the one occupancy read the
    write path that actually refuses selection also uses, so what the agent is
    told is in its way and what genuinely blocks it cannot drift apart.
    """
    from plans.admission_attempts import slot_holders

    return {
        slot: {
            "id": str(row["node_id"]),
            "title": str(row["title"]),
            "state": str(row["state"]),
        }
        for slot, row in slot_holders(db, plan_ids, executors).items()
    }


def condition_from_facts(
    state: str, plan_state: str, *, archived: bool = False, paused: bool = False,
    blocked: bool = False, waiting: bool = False, selected: bool = False,
) -> str:
    """Reduce primitive admission facts into the canonical condition."""
    if archived or state in service._TERMINAL_STATES:
        return "terminal"
    if state == "review_requested":
        return "review"
    if plan_state == "paused" or paused:
        return "paused"
    if blocked:
        return "blocked"
    if waiting:
        return "waiting"
    return "running" if selected else "ready"


def _condition(node, plan, causes: list[dict], attempt) -> str:
    try:
        archived_at = node["archived_at"]
    except (KeyError, IndexError):
        archived_at = None
    return condition_from_causes(
        str(node["state"]), str(plan["state"]), causes,
        archived=archived_at is not None, selected=attempt is not None,
    )


def condition_from_causes(
    state: str, plan_state: str, causes: list[dict], *, selected: bool, archived: bool = False,
) -> str:
    """Canonical cause reduction shared by full admission and narrow consumers."""
    return condition_from_facts(
        state, plan_state, archived=archived,
        paused=any(cause["kind"] == "paused" for cause in causes),
        blocked=any(cause["kind"] == "blocked" for cause in causes),
        waiting=any(cause["kind"] == "waiting" for cause in causes), selected=selected,
    )


def _selection(attempt, live_executors: set[str], in_flight: dict) -> dict | None:
    """Project the shown attempt; `turn_active` means its executor has a live Turn."""
    if attempt is None:
        return None
    executor = str(attempt["executor_thread_id"])
    return {
        "attempt_id": attempt["id"],
        "attempt_ordinal": attempt["attempt_ordinal"],
        "executor_thread_id": executor,
        "requirements_revision": attempt["requirements_revision"],
        "turn_active": executor in live_executors,
        "in_flight_dispatch_count": in_flight.get(str(attempt["id"]), 0),
    }


def _conflict(node, plan, executor, condition, selected) -> dict | None:
    """Project the one selection conflict: another task occupying the slot."""
    # User-assigned work never competes for an executor slot, because
    # admission refuses to select it. Projecting a conflict here would present
    # a human obligation as something the agent must resolve.
    if (
        node["kind"] != "task"
        or condition != "ready"
        or executor is None
        or node["assignee"] == "user"
    ):
        return None
    occupied = selected.get((str(plan["id"]), executor))
    if occupied is None or occupied["id"] == node["id"]:
        return None
    return {
        "condition": "executor_selected",
        "selected_node_id": occupied["id"],
        "selected_node_title": occupied["title"],
        "selected_node_state": occupied["state"],
    }


def _node_causes(node, plan, facts: _Facts):
    """Return one node's current causes and the attempt its projection shows.

    A superseded attempt (``superseded_by``) is never shown: the task does not
    run on a selection it no longer has.
    """
    node_id = str(node["id"])
    causes = [
        *_intrinsic_causes(node, plan, facts.paused_since),
        *facts.holds.get(node_id, []),
        *facts.dependencies.get(node_id, []),
        *facts.watches.get(node_id, []),
        # Waits last: the existing causes keep their precedence, so what
        # `primary_reason` names does not change under this batch.
        *facts.decisions.get(node_id, []),
        *facts.children.get(node_id, []),
    ]
    attempt = facts.attempts.get(node_id)
    if attempt is None:
        return causes, None
    if superseded_by(attempt, node, plan) is None:
        return causes, attempt
    return causes, None


def causes_many(
    node_plans: list[tuple[sqlite3.Row, sqlite3.Row]],
    *,
    db,
    memo=None,
) -> dict[str, list[dict]]:
    """Return the current admission causes of many nodes with a bounded query count.

    Reads only what eligibility needs, not the display projection's Turn and
    slot-occupancy facts.
    """
    pairs = list(node_plans)
    facts = _facts(pairs, db=db, memo=memo)
    return {str(node["id"]): _node_causes(node, plan, facts)[0] for node, plan in pairs}


def project_many(
    node_plans: list[tuple[sqlite3.Row, sqlite3.Row]],
    *,
    db,
    memo=None,
) -> dict[str, dict]:
    """Project admission for any number of nodes with a bounded query count."""
    pairs = list(node_plans)
    facts = _facts(pairs, db=db, memo=memo)
    executors = {
        value
        for node, plan in pairs
        if (value := executor_of(node, plan)) is not None
    }
    live_executors = _live_executors(
        executors | {str(row["executor_thread_id"]) for row in facts.attempts.values()},
        db=db,
    )
    selected = _conflict_index(
        {str(plan["id"]) for _node, plan in pairs},
        executors,
        db=db,
    )
    result = {}
    for node, plan in pairs:
        causes, attempt = _node_causes(node, plan, facts)
        condition = _condition(node, plan, causes, attempt)
        result[str(node["id"])] = {
            "condition": condition,
            "primary_reason": causes[0]["reason"] if causes else None,
            "causes": causes,
            "selection": _selection(attempt, live_executors, facts.in_flight),
            "conflict": _conflict(
                node, plan, executor_of(node, plan), condition, selected,
            ),
        }
    return result


def project(node_id: str, *, db, conflict: dict | None = None) -> dict:
    """Return the canonical admission projection for one node."""

    node = service._node_row(db, node_id)
    plan = service._plan_row(db, node["plan_id"])
    result = project_many([(node, plan)], db=db)[str(node_id)]
    if conflict is not None:
        result["conflict"] = conflict
    return result
