"""Delegation reads cost the child plan's open work and its epoch, never its settled history (#149 S2d).

One worker plan, attached to a parent's step, keeps the same ~20 returned (review) steps
and a few steps settled since the attachment while N settled nodes of older history grow
around it. The budget is SQLite virtual-machine steps (``test_plan_open_tree_scale.measure``):
each delegation read must cost the same at every size. D1 is the harvest's readiness and
its parent-review reads; D2 is worker retirement over the open delegated nodes.

Sizes: ``CORSIA_PLAN_SCALE_SIZES`` (comma-separated settled-node counts); the design's 100,000
is ``CORSIA_PLAN_SCALE_SIZES=0,100000``. The default runs 0 and 5,000.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from plans import delegation, delegation_harvest, mutations, projections, service, worker_retirement
from tests.plan_scale_fixture import _INSERT_EVENT, _INSERT_NODE, _schema, _settled_rows
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_plan_open_tree_scale import SAMPLE_EVERY, SIZES, STEP_TOLERANCE, measure

WORKER = "w1"


def _task(plan_id: str, title: str, parent: str | None, db) -> str:
    return service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria="done", parent_id=parent, db=db,
    ).id


def _review(node_id: str, db) -> None:
    service.transition_node(node_id, "active", db=db)
    mutations.request_review(node_id, evidence="proven", actor_id=WORKER, db=db)


def _build(settled: int) -> tuple:
    """``(db, parent step, child plan id)``: returned work, and ``settled`` older nodes beside it."""
    db = _schema()
    parent_plan = service.ensure_plan("thread", "owner", db=db).id
    step = service.add_node(
        parent_plan, kind="task", title="Delegate", acceptance_criteria="done",
        spawn_kind="claude-code", db=db,
    ).id
    service.transition_node(step, "active", db=db)
    child_plan = service.ensure_plan("thread", WORKER, db=db).id
    mutations.attach_spawned_thread(step, WORKER, db=db)
    phase = service.add_node(
        child_plan, kind="phase", title="Worker phase", acceptance_criteria="done", db=db,
    ).id
    for i in range(15):
        _review(_task(child_plan, f"r{i}", phase, db), db)
    nested = service.add_node(
        child_plan, kind="phase", title="Nested", acceptance_criteria="done", parent_id=phase, db=db,
    ).id
    for i in range(4):
        _review(_task(child_plan, f"n{i}", nested, db), db)
    for i in range(5):  # settled since the attachment
        leaf = _task(child_plan, f"s{i}", phase, db)
        _review(leaf, db)
        service.transition_node(leaf, "accepted", verification_result="passed", verified_by="t", db=db)
    service._event(
        db, step, "child_plan_completed", actor_id=None, provenance="plan.delegation_harvest",
        payload={"child_plan_id": child_plan},
    )
    db.commit()
    nodes, events, _attempts, _usage = _settled_rows(child_plan, phase, settled)
    with raw_plan_seed(db):
        db.executemany(_INSERT_NODE, nodes)
        db.executemany(_INSERT_EVENT, events)
    db.commit()
    return db, step, child_plan


def _reads(db, step_id: str, child_plan: str) -> dict[str, Callable[[], object]]:
    step = service._node_row(db, step_id)
    payload = delegation.attachment_payload(delegation._latest_attachment(step_id, db))
    plan = service._plan_row(db, child_plan)
    return {
        "D1 harvest ready": lambda: delegation_harvest._ready(step, payload, db),
        "D1 delegated_work_returned": lambda: projections.delegated_work_returned(db, child_plan),
        "D1 parent_reviews": lambda: delegation_harvest.parent_reviews(db, plan),
        "D1 returned_steps": lambda: delegation_harvest.returned_steps(db, step),
        "D2 open_delegated_node_ids": lambda: projections.open_delegated_node_ids(db, child_plan),
        "D2 open_delegated_work": lambda: projections.open_delegated_work(db, child_plan),
    }


def _retire(db, child_plan: str) -> int:
    """Worker retirement over the plan's open delegated nodes (the close loop of ``close_open_steps``)."""
    worker_retirement._suppress_open_steps(db, child_plan, None)
    return len(projections.open_delegated_node_ids(db, child_plan))


def collect(sizes=SIZES) -> dict[str, dict[int, int]]:
    """``{read: {size: vm_steps}}``."""
    found: dict[str, dict[int, int]] = {}
    for size in sizes:
        db, step, child_plan = _build(size)
        for name, call in _reads(db, step, child_plan).items():
            call()  # prime statement caches so only steady-state work is counted
            found.setdefault(name, {})[size] = measure(db, call)[0]
        # Retirement closes the open steps, so it runs once, last, and its count is one retirement.
        found.setdefault("D2 worker retirement", {})[size] = measure(db, lambda: _retire(db, child_plan))[0]
        db.close()
    return found


@pytest.fixture(scope="module")
def measured():
    return collect()


READS = [
    "D1 harvest ready", "D1 delegated_work_returned", "D1 parent_reviews", "D1 returned_steps",
    "D2 open_delegated_node_ids", "D2 open_delegated_work", "D2 worker retirement",
]


def _assert_flat(measured, read: str) -> None:
    sizes = measured[read]
    small, large = min(sizes), max(sizes)
    print(f"\n{read}: " + ", ".join(f"{size}: {steps} steps" for size, steps in sorted(sizes.items())))
    assert sizes[large] <= sizes[small] * STEP_TOLERANCE + 2 * SAMPLE_EVERY, (read, sizes)


@pytest.mark.parametrize("read", READS)
def test_delegation_read_cost_is_flat_in_settled_history(measured, read: str) -> None:
    _assert_flat(measured, read)
