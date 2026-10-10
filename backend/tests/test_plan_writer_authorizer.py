"""The test-suite authorizer refuses a raw write of a plan-aggregate input (#731 S1b-2).

A backstop behind the static W gate (``test_plan_aggregate_input_writers``): SQLite
prepares a statement once per connection, so an identical statement first prepared inside
a sanctioned writer is not authorized again. The refusal is therefore best effort, and
these tests pin what it does catch.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from plans.aggregates_recount import stored_mismatches
from tests import plan_writer_authorizer
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


def _file_copy(world, path):
    """``world``'s database as a file, opened (and so tracked) by the gate's ``sqlite3.connect``."""
    world.db.commit()
    copy = sqlite3.connect(path)
    world.db.backup(copy)
    copy.commit()
    return copy


def test_the_recount_names_drift_written_on_the_same_thread(world, tmp_path):
    world.add(None)
    copy = _file_copy(world, tmp_path / "same.db")
    plan_writer_authorizer.recount_changed_plans()
    copy.execute("UPDATE plan_nodes SET sub_open = sub_open + 5")
    copy.commit()
    assert plan_writer_authorizer.recount_changed_plans() != []
    assert plan_writer_authorizer.recount_changed_plans() == []


def test_the_recount_names_drift_a_still_open_connection_on_another_thread_wrote(world, tmp_path):
    world.add(None)
    path = tmp_path / "other.db"
    _file_copy(world, path).close()
    plan_writer_authorizer.recount_changed_plans()
    opened, done = threading.Event(), threading.Event()

    def other_thread():
        copy = sqlite3.connect(path)
        copy.execute("UPDATE plan_nodes SET sub_open = sub_open + 5")
        copy.commit()
        opened.set()
        done.wait(10)  # the connection stays open and owned by this thread
        copy.close()

    worker = threading.Thread(target=other_thread)
    worker.start()
    try:
        assert opened.wait(10)
        assert plan_writer_authorizer.recount_changed_plans() != []
    finally:
        done.set()
        worker.join()
