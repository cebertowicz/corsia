"""The test-suite authorizer refuses a raw write of a plan-aggregate input (#731 S1b-2).

A backstop behind the static W gate (``test_plan_aggregate_input_writers``): SQLite
prepares a statement once per connection, so an identical statement first prepared inside
a sanctioned writer is not authorized again. The refusal is therefore best effort, and
these tests pin what it does catch.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

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


def test_the_recount_names_drift_a_shareable_connection_wrote_from_another_thread(world, tmp_path):
    """A pool-style connection opened here is judged through its file, never read directly."""
    world.add(None)
    path = tmp_path / "shared.db"
    _file_copy(world, path).close()
    plan_writer_authorizer.recount_changed_plans()
    shared = sqlite3.connect(path, check_same_thread=False)

    def other_thread():
        shared.execute("UPDATE plan_nodes SET sub_open = sub_open + 5")
        shared.commit()

    worker = threading.Thread(target=other_thread)
    worker.start()
    worker.join(10)
    try:
        assert plan_writer_authorizer.recount_changed_plans() != []
        assert plan_writer_authorizer.recount_changed_plans() == []
    finally:
        shared.close()


BACKEND = Path(__file__).resolve().parent.parent
#: A shareable connection opened on the recount's thread, mid-statement on a job thread whose
#: statement calls back into Python. Reading the connection from the recount thread waits for
#: its mutex while holding the GIL, and the callback waits for the GIL: neither returns.
_MID_STATEMENT = """
import sqlite3, sys, threading, time
from tests import plan_writer_authorizer
plan_writer_authorizer.install()
shared = sqlite3.connect(sys.argv[1], check_same_thread=False)
shared.execute("CREATE TABLE t (x)")
shared.commit()
shared.create_function("slow", 0, lambda: time.sleep(1) or 1)
started = threading.Event()

def job():
    started.set()
    shared.execute("SELECT slow()").fetchall()

worker = threading.Thread(target=job)
worker.start()
started.wait()
time.sleep(0.3)
plan_writer_authorizer.recount_changed_plans()
worker.join()
print("returned")
"""


def test_the_recount_does_not_wait_on_a_connection_mid_statement_on_another_thread(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    done = subprocess.run([sys.executable, "-c", _MID_STATEMENT, str(tmp_path / "busy.db")], cwd=BACKEND,
                          env=env, capture_output=True, text=True, timeout=30, check=True)
    assert done.stdout.strip() == "returned"
