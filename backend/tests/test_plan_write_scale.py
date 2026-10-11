"""Plan writes cost the open work, never the settled history (#149 S2w).

The same budget as ``test_plan_open_tree_scale``: SQLite virtual-machine steps through
``set_progress_handler``, flat between a plan with no settled history and one with ``N``
(``CORSIA_PLAN_SCALE_SIZES``; the design's 100,000 is ``0,100000``). Each write path is
run once to prime statement caches and then measured on an equivalent second call.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest

from plans import admission, close_next, mutations, node_order, selection_validity, service, triage_lines
from tests.plan_scale_fixture import Scale, add_ready_branch, build
from tests.test_plan_open_tree_scale import SAMPLE_EVERY, SIZES, STEP_TOLERANCE, measure

ACTOR = "owner"


def _paths(scale: Scale) -> dict[str, Callable[[], object]]:
    """Each write path as a repeatable call; a pair of calls undoes itself where it must."""
    db = scale.db
    plan = service._plan_row(db, scale.plan_id)
    top_leaves = [row["id"] for row in db.execute(
        "SELECT id FROM plan_nodes WHERE plan_id=? AND parent_id IS NULL AND title LIKE 'c%' "
        "ORDER BY order_key,id", (scale.plan_id,))]
    swap = iter([2, 3] * 4)
    ready = iter([add_ready_branch(scale, scale.size, f"{i}") for i in range(2)])
    return {
        "W1 reconcile_plan (thread plan)": lambda: admission.reconcile_plan(
            scale.plan_id, actor_id=ACTOR, provenance="t", db=db),
        "W1 first eligible active task": lambda: selection_validity.first_eligible_active_id(db, plan),
        "W1 active_task_ids (scope plan)": lambda: node_order.active_task_ids(db, scale.plan_id),
        "W2 close_next executor_next": lambda: close_next.executor_next(db, scale.plan_id, ACTOR),
        "W5 triage standing": lambda: triage_lines.standing(db, plan),
        "W5 triage position": lambda: triage_lines.position(db, scale.plan_id, top_leaves[0]),
        "W4 accept a finished branch": lambda: service.transition_node(
            next(ready), "accepted", verification_result="passed", verified_by="t", db=db),
        "W3 reorder": lambda: mutations.reorder_node(top_leaves[4], next(swap), db=db),
        "W3 insert at the top": lambda: service.add_node(
            scale.plan_id, kind="task", title="inserted", acceptance_criteria="done",
            order_key=0, db=db),
    }


def collect(sizes=SIZES) -> dict[str, dict[int, int]]:
    found: dict[str, dict[int, int]] = {}
    for size in sizes:
        scale = build(size)
        for name, call in _paths(scale).items():
            call()  # primes statement caches so only steady-state work is counted
            steps, _result = measure(scale.db, call)
            found.setdefault(name, {})[size] = steps
        scale.db.close()
    return found


@pytest.fixture(scope="module")
def measured():
    return collect()


@pytest.mark.parametrize("path", [
    "W1 reconcile_plan (thread plan)", "W1 first eligible active task",
    "W1 active_task_ids (scope plan)", "W2 close_next executor_next",
    "W5 triage standing", "W5 triage position", "W4 accept a finished branch",
    "W3 reorder", "W3 insert at the top",
])
def test_write_cost_is_flat_in_settled_history(measured, path: str) -> None:
    sizes = measured[path]
    small, large = min(sizes), max(sizes)
    print(f"\n{path}: " + ", ".join(f"{size}: {steps} steps" for size, steps in sorted(sizes.items())))
    assert sizes[large] <= sizes[small] * STEP_TOLERANCE + 2 * SAMPLE_EVERY, (path, sizes)
