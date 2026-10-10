"""A schema step's plan events are no one's news (``plan_events.from_schema_step``).

Every upgrade moves old data through the runtime's own writers; the one rule
at the event fan-out keeps each of them from telling an owner or a parent
about state that already existed.
"""

from __future__ import annotations

from contextlib import closing

from plans import plan_events, service
from tests.child_parent_notice_helpers import delegated, notices, seeded_db, transcript, wakes
from tests.plan_writer_authorizer import raw_plan_seed


def _fail_top_level(db, *, provenance: str) -> None:
    plan = service.ensure_plan("thread", "parent", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Milestone", acceptance_criteria="Done", db=db,
    )
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='failed' WHERE id=?", (node.id,))
    service._event(
        db, node.id, "state_transition", actor_id=None, provenance=provenance,
        prior_state="active", new_state="failed",
    )


def test_a_runtime_milestone_tells_its_owner():
    """The control: the same write outside a schema step is the owner's news."""
    with closing(seeded_db()) as db:
        before = len(transcript(db))
        _fail_top_level(db, provenance="tool.plans")
        assert len(transcript(db)) == before + 1


def test_a_schema_step_milestone_tells_no_one():
    with closing(seeded_db()) as db:
        before = len(transcript(db))
        _fail_top_level(db, provenance="migration.56_to_57")
        assert len(transcript(db)) == before


def test_a_schema_step_resume_tells_no_one():
    with closing(seeded_db()) as db:
        plan = service.ensure_plan("thread", "parent", db=db)
        before = len(transcript(db))
        plan_events.append(
            db, plan.id, "state_transition", actor_id=None,
            provenance="migration.58_to_59", prior_state="paused", new_state="active",
        )
        assert len(transcript(db)) == before


def test_a_schema_step_worker_event_neither_wakes_nor_tells_the_parent():
    with closing(seeded_db()) as db:
        _step, _plan, node = delegated(db)
        told, woken = len(notices(db)), len(wakes(db))
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (node.id,))
        service._event(
            db, node.id, "state_transition", actor_id=None,
            provenance="migration.56_to_57", prior_state="failed", new_state="closed",
            payload={"reason": "Sent back"},
        )
        assert (len(notices(db)), len(wakes(db))) == (told, woken)
