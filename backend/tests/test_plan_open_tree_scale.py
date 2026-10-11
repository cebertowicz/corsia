"""Agent plan reads cost the open work, never the settled history (#731 S2-1).

Design r6 section 6 (scale test). The budget is SQLite virtual-machine steps, counted
through ``set_progress_handler``: deterministic, and it sees in-SQL O(N) work that a
SELECT count misses. One plan keeps the same ~20 open rows while its settled history
grows; each converted read must cost the same at every size.

Tolerance: ``STEP_TOLERANCE`` (10%) over the smallest size. The two sources of honest
growth are the depth of the b-trees an indexed lookup descends (a handful of steps per
probe) and the digits in a count the page prints, both far under 1%; a read that
touches history grows by orders of magnitude, so 10% separates the two with room.

Sizes: ``CORSIA_PLAN_SCALE_SIZES`` (comma-separated settled-node counts). The default
runs 0 and 5,000 in a few seconds; the design's 100,000 is ``CORSIA_PLAN_SCALE_SIZES=0,100000``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

from plans import (
    activity_facts, current, folded_reads, operator_chat_state, operator_node_facts,
    operator_projection, projections, service, upkeep_notice,
)
from plans.boundary import PlanActor
from tests.plan_scale_fixture import Scale, build

SIZES = tuple(int(size) for size in os.environ.get("CORSIA_PLAN_SCALE_SIZES", "0,5000").split(","))
STEP_TOLERANCE = 1.10
PAYLOAD_TOLERANCE = 1.10
ACTOR = PlanActor("owner", "user-1", "p")
HUMAN = PlanActor(None, "user-1", None, kind="user")
SAMPLE_EVERY = 50


def measure(db, call: Callable[[], object]) -> tuple[int, object]:
    """Run ``call`` and return its SQLite VM step count (to SAMPLE_EVERY) and result."""
    ticks = 0

    def tick() -> int:
        nonlocal ticks
        ticks += 1
        return 0

    db.set_progress_handler(tick, SAMPLE_EVERY)
    try:
        result = call()
    finally:
        db.set_progress_handler(None, 0)
    return ticks * SAMPLE_EVERY, result


@contextmanager
def without_costs() -> Iterator[None]:
    """The pane with its cost roll-ups left unread (``project_costs(wanted=False)``)."""
    real = operator_node_facts.project_costs

    def uncosted(node_plans, *, db, now, wanted=True, detail_ids=None):
        return real(node_plans, db=db, now=now, wanted=False, detail_ids=detail_ids)

    operator_node_facts.project_costs = uncosted
    try:
        yield
    finally:
        operator_node_facts.project_costs = real


def _pane_without_costs(db) -> object:
    with without_costs():
        return operator_projection.read_thread("owner", actor=HUMAN, db=db)


def _reads(scale: Scale) -> dict[str, Callable[[], object]]:
    db = scale.db
    return {
        "R2 status": lambda: current.read_current_status(actor=ACTOR, db=db),
        "R2 plan_read(node_id) leaf": lambda: current.read_current_status(
            actor=ACTOR, node_id=scale.open_leaf, db=db),
        "R2 plan_read(node_id) container": lambda: current.read_current_status(
            actor=ACTOR, node_id=scale.open_container, db=db),
        "R3 plan_read full": lambda: current.read_current_plan(actor=ACTOR, db=db),
        "R4 plan_state": lambda: projections.plan_state(scale.plan_id, db=db),
        "R4 liveness_signal": lambda: projections.liveness_signal("owner", db=db),
        "R1 pane read_thread": lambda: operator_projection.read_thread("owner", actor=HUMAN, db=db),
        "R1 pane without costs": lambda: _pane_without_costs(db),
        "R5 operator read_child": lambda: operator_projection.read_child(scale.worker_step, actor=HUMAN, db=db),
        "R6 chat list": lambda: operator_chat_state.read_thread_states(["owner"], actor=HUMAN, db=db),
        "R7 upkeep notice": lambda: upkeep_notice.notice_for(db, "owner", "bash"),
        "R8 activity index": lambda: activity_facts.compact_nodes([scale.plan_id], db=db),
        "route folded page": lambda: folded_reads.read_folded(scale.plan_id, actor=HUMAN, db=db),
        "route path of an open node": lambda: folded_reads.read_path(
            scale.plan_id, scale.open_leaf, actor=HUMAN, db=db),
    }


def _payload(result: object) -> int:
    return len(json.dumps(result, default=lambda value: getattr(value, "__dict__", str(value)), sort_keys=True))


def collect(sizes=SIZES) -> dict[str, dict[int, tuple[int, int]]]:
    """``{read: {size: (vm_steps, payload_bytes)}}``."""
    found: dict[str, dict[int, tuple[int, int]]] = {}
    for size in sizes:
        scale = build(size)
        service._now()  # warm nothing; the clock is read per call
        for name, call in _reads(scale).items():
            call()  # first call primes statement caches so only steady-state work is counted
            steps, result = measure(scale.db, call)
            found.setdefault(name, {})[size] = (steps, _payload(result))
        scale.db.close()
    return found


@pytest.fixture(scope="module")
def measured():
    return collect()


FLAT = [
    "R2 status", "R2 plan_read(node_id) leaf", "R2 plan_read(node_id) container",
    "R3 plan_read full", "R4 plan_state", "R4 liveness_signal",
    "R5 operator read_child", "R7 upkeep notice", "R8 activity index",
    "route path of an open node", "R1 pane without costs", "R6 chat list",
]
#: Reads that still grow with settled history, each for one named cause outside S2-2, so
#: the test fails (strict xfail) the day the cause is fixed and the read joins ``FLAT``.
KNOWN_GROWTH = {
    "R1 pane read_thread": (
        "a listed folded container is costed over its whole subtree until S5 stores rolled cost "
        "(commitment_cost, node_cost_scoped, node_waiting); the pane without costs is in FLAT"
    ),
}


def _assert_flat(measured, read: str) -> None:
    sizes = measured[read]
    small, large = min(sizes), max(sizes)
    lines = ", ".join(f"{size}: {steps} steps / {payload} B" for size, (steps, payload) in sorted(sizes.items()))
    print(f"\n{read}: {lines}")
    # Steps are sampled every SAMPLE_EVERY, so a read of a few hundred steps can swing by two samples.
    assert sizes[large][0] <= sizes[small][0] * STEP_TOLERANCE + 2 * SAMPLE_EVERY, (read, sizes)
    assert sizes[large][1] <= sizes[small][1] * PAYLOAD_TOLERANCE, (read, sizes)


@pytest.mark.parametrize("read", FLAT)
def test_read_cost_is_flat_in_settled_history(measured, read: str) -> None:
    _assert_flat(measured, read)


@pytest.mark.parametrize("read", [
    pytest.param(read, marks=pytest.mark.xfail(strict=True, reason=reason))
    for read, reason in KNOWN_GROWTH.items()
])
def test_reads_with_a_named_cause_still_grow(measured, read: str) -> None:
    _assert_flat(measured, read)
