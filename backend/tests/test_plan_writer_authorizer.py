"""The test-suite authorizer refuses a raw write of a plan-aggregate input (#731 S1b-2).

A backstop behind the static W gate (``test_plan_aggregate_input_writers``): SQLite
prepares a statement once per connection, so an identical statement first prepared inside
a sanctioned writer is not authorized again. The refusal is therefore best effort, and
these tests pin what it does catch.
"""

from __future__ import annotations

import pytest

from plans.aggregates_recount import stored_mismatches
from tests.plan_writer_authorizer import PlanWriterRefused, raw_plan_seed
from tests.test_plan_aggregates_property import World


@pytest.fixture
def world():
    return World()


def test_a_raw_write_of_an_input_column_is_refused_with_the_routes(world):
    node = world.add(None)
    for sql in (
        "UPDATE plan_nodes SET state='accepted' WHERE id=?",
        "UPDATE plan_nodes SET archived_at='t' WHERE id=?",
        "DELETE FROM plan_nodes WHERE id=?",
    ):
        with pytest.raises(PlanWriterRefused, match="raw_plan_seed"):
            world.db.execute(sql, (node,))


def test_a_raw_insert_is_refused(world):
    with pytest.raises(PlanWriterRefused, match="INSERT"):
        world.db.execute(
            "INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,"
            "commitment_id,created_at,updated_at) VALUES ('x',?,0,'task','t','d','pending','x','t','t')",
            (world.plan.id,),
        )


def test_a_column_that_is_not_an_input_is_not_refused(world):
    node = world.add(None)
    world.db.execute("UPDATE plan_nodes SET title='renamed' WHERE id=?", (node,))


def test_a_seed_block_lifts_the_refusal_and_recounts_on_exit(world):
    node = world.add(None)
    with raw_plan_seed(world.db):
        world.db.execute("UPDATE plan_nodes SET state='failed' WHERE id=?", (node,))
    assert stored_mismatches(world.db, world.plan.id) == []
