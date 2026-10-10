"""The open-rows write paths against the full reads they replaced (#149 S2w).

Random sequences of real plan operations (the generator of ``test_plan_aggregates_property``),
and after every step the open-only answers must equal the answers of the whole-plan read:
the open nodes in plan order, the active tasks, and the unsettled-descendant verdicts a
close or an acceptance would give. Reorder and insert position among open rows are checked
by the order the open read then lists. Sequences are seeded; ``CORSIA_PLAN_PROPERTY_SEQUENCES``
raises the count.
"""

from __future__ import annotations

import random

from plans import archival, node_order, node_states, review_verification, service
from tests.test_plan_aggregates_property import MAX_OPS, SEQUENCES, World, _op


def _retired_above(by_id: dict, path: tuple[str, ...]) -> bool:
    return any(a in by_id and node_states.retired(by_id[a]) for a in path)


def _full_open(world: World) -> list[str]:
    """Open nodes in plan order, from the full read."""
    ordered = node_order.ordered_nodes_with_paths(world.db, world.plan.id)
    by_id = {str(row["id"]): row for row, _ in ordered}
    return [
        str(row["id"]) for row, path in ordered
        if row["state"] not in node_states._TERMINAL_STATES and not _retired_above(by_id, path)
    ]


def _full_active(world: World) -> list[str]:
    ordered = node_order.ordered_nodes_with_paths(world.db, world.plan.id)
    return [str(r["id"]) for r, _ in ordered if r["kind"] == "task" and r["state"] == "active"]


def _verdicts(world: World, node_id: str) -> tuple:
    node = service._node_row(world.db, node_id)

    def refused(call) -> str | bool:
        try:
            call()
        except service.PlanError as error:
            return str(error)
        return False

    return (
        refused(lambda: archival._require_no_open_children(world.db, node_id)),
        refused(lambda: review_verification._require_children_settled(world.db, node)),
    )


def _walked_verdicts(world: World, node_id: str) -> tuple:
    node = service._node_row(world.db, node_id)
    open_children = archival._walk_open_children(world.db, node_id)
    return bool(open_children), review_verification._walk_unsettled(world.db, node)


def test_open_reads_equal_the_full_reads_after_every_operation() -> None:
    for seed in range(SEQUENCES):
        rng = random.Random(seed)
        world = World()
        for _ in range(rng.randint(1, MAX_OPS)):
            _op(world, rng)
            world.db.commit()
            where = (seed, world.log[-3:])
            opened = [str(r["id"]) for r, _ in node_order.open_nodes_with_paths(world.db, world.plan.id)]
            assert opened == _full_open(world), where
            assert node_order.active_task_ids(world.db, world.plan.id) == _full_active(world), where
            for row in world.rows():
                node_id = str(row["id"])
                refusals = _verdicts(world, node_id)
                walked = _walked_verdicts(world, node_id)
                assert bool(refusals[0]) == walked[0], (node_id, where)
                # acceptance also refuses on failed work, which the walk counts as unsettled
                assert bool(refusals[1]) == walked[1], (node_id, where)
