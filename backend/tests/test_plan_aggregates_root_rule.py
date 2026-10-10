"""One root rule for the stored aggregates (#731 S2-1).

A live row under an archived parent is a root: ``aggregates.apply``, ``aggregates_rebuild``
and the recount (``aggregates_recount``) must agree on it. The incremental write used to walk
``parent_id`` through the archived ancestor and push the row's change into it.
"""

from __future__ import annotations

from plans import aggregates_rebuild, service
from plans.aggregates_recount import stored_mismatches
from tests.test_plan_aggregates_property import World

_NODE = (
    "id", "listed", "sub_open", "sub_done", "sub_in_work", "prop_open", "prop_done", "prop_in_work",
    "folded", "fold_bucket", "child_in_work", "child_folded_done", "child_folded_failed",
)
_PLAN = ("top_open", "top_done", "top_failed", "top_in_work", "top_roots", "top_folded_done")


def _stored(world: World) -> tuple[list[tuple], tuple]:
    nodes = [
        tuple(row[name] for name in _NODE)
        for row in world.db.execute(
            "SELECT * FROM plan_nodes WHERE plan_id=? ORDER BY id", (world.plan.id,))
    ]
    plan = world.db.execute("SELECT * FROM plans WHERE id=?", (world.plan.id,)).fetchone()
    return nodes, tuple(plan[name] for name in _PLAN)


def test_a_change_under_an_archived_parent_reads_as_rebuild_and_the_recount_read_it() -> None:
    world = World()
    top = world.add(None, "phase")
    assert top
    parent = world.add(top, "phase")
    assert parent
    child = world.add(parent)
    other = world.add(parent)
    assert child and other
    # An archived parent that still has live children: the state a restore or a legacy row leaves.
    service.touch_node(world.db, parent, at=service._now(), archived_at=service._now())
    aggregates_rebuild.rebuild(world.db, world.plan.id)
    world.db.commit()
    assert not stored_mismatches(world.db, world.plan.id)

    # Real operations: ``apply`` walks up from the child and settles it.
    assert world.start(child) and world.review(child) and world.accept(child)
    after_apply = _stored(world)
    assert not stored_mismatches(world.db, world.plan.id)

    aggregates_rebuild.rebuild(world.db, world.plan.id)
    assert _stored(world) == after_apply
