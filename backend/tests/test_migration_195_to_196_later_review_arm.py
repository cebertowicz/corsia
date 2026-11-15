"""Schema 195 -> 196 arms the Later piles made before the weekly review existed."""

import sqlite3
from datetime import datetime, timedelta, timezone

from infra.database_migrations import MIGRATIONS
from infra.migration_195_to_196_later_review_arm import first_review_after, migrate_195_to_196
from infra.plan_schema import STRUCTURED_PLAN_SCHEMA_SQL
from infra.schema import SCHEMA_VERSION
from plans import later_review
from tests.plan_writer_authorizer import raw_plan_seed

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
CREATED = "2026-11-09T08:00:00+00:00"  # a Monday, 6 days and 2 hours before NOW


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(STRUCTURED_PLAN_SCHEMA_SQL)
    return db


def _plan(db, plan_id, *, owner_kind="thread", state="active", armed=None):
    db.execute(
        "INSERT INTO plans (id, owner_kind, owner_id, state, created_at, updated_at, later_review_at) "
        "VALUES (?, ?, ?, ?, 'now', 'now', ?)", (plan_id, owner_kind, f"owner-{plan_id}", state, armed),
    )


def _task(db, node_id, plan_id, *, later=1, state="pending", created=CREATED, archived=None, kind="task"):
    """A row as a pre-196 install holds it: below the cut, its plan never armed."""
    with raw_plan_seed(db):
        _insert_task(db, node_id, plan_id, later, state, created, archived, kind)


def _insert_task(db, node_id, plan_id, later, state, created, archived, kind):
    db.execute(
        "INSERT INTO plan_nodes (id, plan_id, order_key, kind, title, acceptance_criteria, later, state, "
        "commitment_id, created_at, updated_at, archived_at) "
        "VALUES (?, ?, 0, ?, 't', 'done', ?, ?, ?, ?, 'now', ?)",
        (node_id, plan_id, kind, later, state, node_id, created, archived),
    )


def _armed(db, plan_id):
    return db.execute("SELECT later_review_at FROM plans WHERE id=?", (plan_id,)).fetchone()[0]


def test_registered_as_the_current_step():
    assert SCHEMA_VERSION == 196
    assert MIGRATIONS[195] is migrate_195_to_196


def test_unarmed_pile_gets_the_next_weekly_time_on_its_oldest_task():
    db = _db()
    _plan(db, "p")
    _task(db, "older", "p", created=CREATED)
    _task(db, "newer", "p", created="2026-11-14T23:00:00+00:00")
    migrate_195_to_196(db, now=NOW)
    assert _armed(db, "p") == "2026-11-16T08:00:00+00:00"
    # The review reader now finds it once its time comes, and not before.
    assert [row["id"] for row in later_review.due(db, "2026-11-16T08:00:00+00:00")] == ["p"]
    assert later_review.due(db, NOW.isoformat()) == []


def test_old_pile_lands_after_now_on_the_same_weekday():
    assert first_review_after(datetime(2026, 10, 1, 9, tzinfo=timezone.utc), NOW) == datetime(
        2026, 11, 19, 9, tzinfo=timezone.utc)
    assert first_review_after(NOW - timedelta(days=7), NOW) == NOW + timedelta(days=7)
    assert first_review_after(None, NOW) == NOW + timedelta(days=7)
    assert first_review_after(NOW + timedelta(hours=1), NOW) == NOW + timedelta(days=7)


def test_only_unarmed_thread_plans_with_an_open_later_task_move():
    db = _db()
    _plan(db, "armed", armed="2026-11-20T00:00:00+00:00")
    _task(db, "a", "armed")
    _plan(db, "above-cut")
    _task(db, "b", "above-cut", later=0)
    _plan(db, "settled")
    _task(db, "c", "settled", state="accepted")
    _task(db, "c2", "settled", state="closed")
    _plan(db, "archived-task")
    _task(db, "d", "archived-task", archived="2026-11-10T00:00:00+00:00")
    _plan(db, "scope", owner_kind="project_scope")
    _task(db, "e", "scope")
    _plan(db, "branch-only")
    _task(db, "f", "branch-only", kind="phase")
    _plan(db, "paused", state="paused")
    _task(db, "g", "paused", state="review_requested")
    migrate_195_to_196(db, now=NOW)
    assert _armed(db, "armed") == "2026-11-20T00:00:00+00:00"
    for untouched in ("above-cut", "settled", "archived-task", "scope", "branch-only"):
        assert _armed(db, untouched) is None, untouched
    # Armed as arm() would, whatever the plan's state; due() still skips it until it is active.
    assert _armed(db, "paused") == "2026-11-16T08:00:00+00:00"


def test_rerun_moves_nothing_and_a_db_without_plans_is_left_alone():
    db = _db()
    _plan(db, "p")
    _task(db, "t", "p")
    migrate_195_to_196(db, now=NOW)
    first = _armed(db, "p")
    migrate_195_to_196(db, now=NOW + timedelta(days=30))
    assert _armed(db, "p") == first
    empty = sqlite3.connect(":memory:")
    migrate_195_to_196(empty, now=NOW)
    assert empty.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
