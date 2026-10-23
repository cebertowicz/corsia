"""Sent-back plan rejections become correction evidence, written with the rejection."""

from __future__ import annotations

import json
import sqlite3

import pytest

from infra.database_migrations import MIGRATIONS
from infra.migration_143_to_144_sent_back_corrections import migrate_143_to_144
from infra.schema import SCHEMA_VERSION
from plans import service
from tests.test_plan_rejection_revision import _db as rejection_db, _reject, _returned

FEEDBACK = "The implementation misses the empty input path."


def _facts(db) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT subject_key,agent_thread_id,project_id,attributes FROM outcome_events "
        "WHERE event_name='measurement.correction' ORDER BY subject_key"
    ).fetchall()


def _rejection_ids(db, node_id: str) -> list[str]:
    return [
        row["id"] for row in db.execute(
            "SELECT id FROM plan_node_events WHERE node_id=? AND event='review_rejected'", (node_id,),
        )
    ]


def _seed_rejection(db, runner: str, *, revision: bool = True) -> str:
    """A node in ``runner``'s plan that was rejected, with the revision a rejection creates."""
    plan = service.ensure_plan("thread", runner, db=db)
    node = service.add_node(plan.id, kind="task", title="Task", acceptance_criteria="Works", db=db)
    events = [("review_rejected", node.id, f"reject-{node.id}")]
    if revision:
        follow_up = service.add_node(plan.id, kind="task", title="Revision", acceptance_criteria="Works", db=db)
        db.execute("UPDATE plan_nodes SET follow_up_of=? WHERE id=?", (node.id, follow_up.id))
        events.append(("revision_created", follow_up.id, f"revision-{node.id}"))
    for event, node_id, event_id in events:
        db.execute(
            "INSERT INTO plan_node_events (id,node_id,actor_id,provenance,event,payload_json,created_at) "
            "VALUES (?,?,'parent','test',?,?,'2026-10-01T12:00:00+00:00')",
            (event_id, node_id, event, json.dumps({"feedback": FEEDBACK})),
        )
    db.commit()
    return events[0][2]


def test_a_boundary_rejection_writes_its_fact_in_the_same_transaction():
    db = rejection_db(None)
    try:
        plan, delegated, _worker_plan, leaves = _returned(db, "Worker step", "Second worker step")
        _reject(db, plan.id, delegated.id)

        [reviewed_event] = _rejection_ids(db, delegated.id)
        assert all(len(_rejection_ids(db, leaf.id)) == 1 for leaf in leaves)
        [fact] = _facts(db)
        assert fact["subject_key"] == f"plan-review-rejected:{reviewed_event}"
        assert (fact["agent_thread_id"], fact["project_id"]) == ("parent", "project-1")
        assert json.loads(fact["attributes"]) == {
            "source_kind": "sent_back", "source_id": reviewed_event, "thread_id": "parent",
            "label": "correction",
        }
        assert FEEDBACK not in " ".join(str(value) for value in fact)
        assert db.execute("SELECT dedupe_key FROM outcome_events").fetchone()[0] == (
            f"correction:sent_back:{reviewed_event}"
        )
    finally:
        db.close()


def test_a_fact_that_cannot_be_written_rolls_the_rejection_back(monkeypatch):
    db = rejection_db(None)
    try:
        plan, delegated, _worker_plan, _leaves = _returned(db, "Worker step")

        def refuse(*_args, **_kwargs):
            raise sqlite3.OperationalError("measurement unavailable")

        monkeypatch.setattr("infra.outcome_events.record_outcome_event", refuse)
        with pytest.raises(sqlite3.OperationalError):
            _reject(db, plan.id, delegated.id)

        assert _rejection_ids(db, delegated.id) == []
        assert db.execute("SELECT state FROM plan_nodes WHERE id=?", (delegated.id,)).fetchone()[0] == "review_requested"
        assert db.execute("SELECT COUNT(*) FROM plan_nodes WHERE follow_up_of=?", (delegated.id,)).fetchone()[0] == 0
        assert _facts(db) == []
    finally:
        db.close()


def test_a_fact_counts_against_the_nearest_agent_and_never_drops_an_unresolvable_owner():
    from core.sent_back_corrections import record_rejection_correction

    db = rejection_db(None)
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,project_id,spawned_by,agent_enabled,model,created_at,updated_at) VALUES "
            "('promoted','user-1','project-1','parent',1,'m','t','t'),"
            "('promoted-worker','user-1','project-1','promoted',0,'m','t','t'),"
            "('loop-a','user-1','project-1',NULL,0,'m','t','t'),"
            "('loop-b','user-1','project-1','loop-a',0,'m','t','t')"
        )
        # Seed every plan first: building one walks the spawn chain, which a cycle would never end.
        expected = {
            "parent": ("parent", "project-1"),
            "worker": ("parent", "project-1"),  # a worker counts against the agent that spawned it
            "promoted": ("promoted", "project-1"),  # an agent spawned by another stays itself
            "promoted-worker": ("promoted", "project-1"),
            "loop-a": ("loop-a", "project-1"),  # no agent above it (a cycle): the executor itself
            "ghost": ("ghost", None),  # no thread row at all
        }
        events = {runner: _seed_rejection(db, runner) for runner in expected}
        db.execute("UPDATE threads SET spawned_by='loop-b' WHERE id='loop-a'")
        for runner, want in expected.items():
            event_id = events[runner]
            assert record_rejection_correction(db, event_id)
            row = db.execute(
                "SELECT agent_thread_id,project_id FROM outcome_events WHERE subject_key=?",
                (f"plan-review-rejected:{event_id}",),
            ).fetchone()
            assert tuple(row) == want, runner
    finally:
        db.close()


def test_the_migration_records_each_earlier_verdict_once_and_is_idempotent():
    db = rejection_db(None)
    try:
        first = _seed_rejection(db, "worker")
        second = _seed_rejection(db, "worker")
        _seed_rejection(db, "worker", revision=False)  # a returned leaf is not a verdict

        migrate_143_to_144(db)
        assert [row["subject_key"] for row in _facts(db)] == sorted(
            f"plan-review-rejected:{event}" for event in (first, second)
        )
        migrate_143_to_144(db)
        assert len(_facts(db)) == 2
        assert {row["agent_thread_id"] for row in _facts(db)} == {"parent"}
        assert db.row_factory is sqlite3.Row
    finally:
        db.close()


def test_the_migration_leaves_a_live_fact_alone_and_the_step_is_registered():
    from core.sent_back_corrections import record_rejection_correction

    db = rejection_db(None)
    try:
        assert record_rejection_correction(db, _seed_rejection(db, "worker"))
        migrate_143_to_144(db)
        assert len(_facts(db)) == 1
        assert SCHEMA_VERSION == 144 and MIGRATIONS[143] is migrate_143_to_144
    finally:
        db.close()


def test_sent_back_facts_are_split_by_source_and_do_not_enter_trial_verdicts():
    from core.correction_attribution import attributed_facts
    from core.sent_back_corrections import record_rejection_correction

    db = rejection_db(None)
    try:
        assert record_rejection_correction(db, _seed_rejection(db, "worker"))
        rows = db.execute(
            """SELECT json_extract(attributes, '$.source_kind') AS source_kind, COUNT(*) AS n
               FROM outcome_events WHERE event_name='measurement.correction'
               GROUP BY source_kind"""
        ).fetchall()
        assert [(row["source_kind"], row["n"]) for row in rows] == [("sent_back", 1)]
        assert attributed_facts(db) == []
    finally:
        db.close()


def test_a_v143_install_records_its_earlier_verdicts_when_it_starts():
    from infra.database_migrations import migrate_to_current

    db = rejection_db(None)
    try:
        event_id = _seed_rejection(db, "worker")
        db.execute("PRAGMA user_version=143")
        db.commit()

        assert migrate_to_current(db, SCHEMA_VERSION).migrated
        assert [row["subject_key"] for row in _facts(db)] == [f"plan-review-rejected:{event_id}"]
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
    finally:
        db.close()
