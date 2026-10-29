"""Self-caught plan corrections become transactional measurement facts."""

import json
import sqlite3

import pytest

from plans import service
from tests.test_plan_settlement_correction import (
    db, _accept, _activate, _mutate, _review, _task,
)


def test_correction_records_the_actor_and_event_in_the_plan_transaction(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    source = _task(db, plan.id, "Original")
    active = _activate(db, plan.id, source, "activate-source")
    reviewed = _review(db, plan.id, active["node"], "review-source")
    accepted = _accept(db, plan.id, reviewed["node"], "accept-source")["node"]
    affected = _task(db, plan.id, "Affected")
    result = _mutate(db, plan.id, "add_correction", {
        "node_id": source.id, "title": "Correct", "acceptance_criteria": "Fixed",
        "affected_nodes": [{"node_id": affected.id, "expected_revision": affected.revision}],
        "expected_revision": accepted["revision"],
    }, "correct")
    event = db.execute("SELECT id FROM plan_node_events WHERE node_id=? AND event='correction_created'",
                       (result["correction"]["id"],)).fetchone()[0]
    fact = db.execute("SELECT subject_key,agent_thread_id,attributes FROM outcome_events "
                      "WHERE event_name='measurement.correction'").fetchone()
    assert fact is not None
    assert fact[0] == f"plan-correction-created:{event}"
    assert fact[1] == "owner"
    assert json.loads(fact[2]) == {"source_kind": "self_caught", "source_id": event,
                                  "thread_id": "owner", "label": "correction"}


def test_failure_to_write_fact_rolls_back_the_correction(db, monkeypatch):
    plan = service.ensure_plan("thread", "owner", db=db)
    source = _task(db, plan.id, "Original")
    active = _activate(db, plan.id, source, "activate-source")
    reviewed = _review(db, plan.id, active["node"], "review-source")
    accepted = _accept(db, plan.id, reviewed["node"], "accept-source")["node"]
    affected = _task(db, plan.id, "Affected")

    def refuse(*_args, **_kwargs):
        raise sqlite3.OperationalError("measurement unavailable")

    monkeypatch.setattr("infra.outcome_events.record_outcome_event", refuse)
    with pytest.raises(sqlite3.OperationalError, match="measurement unavailable"):
        _mutate(db, plan.id, "add_correction", {
            "node_id": source.id, "title": "Correct", "acceptance_criteria": "Fixed",
            "affected_nodes": [{"node_id": affected.id, "expected_revision": affected.revision}],
            "expected_revision": accepted["revision"],
        }, "failed-correction")
    assert db.execute("SELECT COUNT(*) FROM plan_nodes WHERE follow_up_of=?", (source.id,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM plan_node_events WHERE event='correction_created'").fetchone()[0] == 0


def _seed_event(db, *, actor="owner"):
    plan = service.ensure_plan("thread", "owner", db=db)
    source = _task(db, plan.id, "Original")
    follow = _task(db, plan.id, "Correction")
    db.execute("UPDATE plan_nodes SET follow_up_of=?,commitment_id=? WHERE id=?",
               (source.id, source.commitment_id, follow.id))
    event = service._event(db, follow.id, "correction_created", actor_id=actor, provenance="test",
                           payload={"source_node_id": source.id, "affected_node_ids": [],
                                    "commitment_id": source.commitment_id})
    return source, event


def test_migration_replays_once_and_ignores_non_thread_actor(db):
    from infra.migration_150_to_151_self_caught_corrections import migrate_150_to_151

    _, event = _seed_event(db, actor="worker")
    _seed_event(db, actor="person")
    migrate_150_to_151(db)
    migrate_150_to_151(db)
    facts = db.execute("SELECT subject_key, agent_thread_id FROM outcome_events").fetchall()
    assert len(facts) == 1
    assert facts[0][0] == f"plan-correction-created:{event}"
    assert facts[0][1] == "owner"


def test_sent_back_on_same_node_or_commitment_suppresses_self_caught(db, monkeypatch):
    from core.self_caught_corrections import record_self_caught_correction
    from core.sent_back_corrections import record_rejection_correction

    source, event = _seed_event(db)
    monkeypatch.setattr(service, "_now", lambda: "2000-01-01T00:00:00+00:00")
    rejection = service._event(db, source.id, "review_rejected", actor_id="owner", provenance="test")
    assert record_rejection_correction(db, rejection)
    assert not record_self_caught_correction(db, event)
    db.execute("UPDATE outcome_events SET retracted_at='now' WHERE subject_key=?",
               (f"plan-review-rejected:{rejection}",))
    assert record_self_caught_correction(db, event)


def test_non_sent_back_fact_on_rejection_subject_does_not_suppress(db, monkeypatch):
    from core.self_caught_corrections import record_self_caught_correction
    from core.sent_back_corrections import record_rejection_correction

    source, event = _seed_event(db)
    monkeypatch.setattr(service, "_now", lambda: "2000-01-01T00:00:00+00:00")
    rejection = service._event(db, source.id, "review_rejected", actor_id="owner", provenance="test")
    assert record_rejection_correction(db, rejection)
    db.execute("UPDATE outcome_events SET attributes=json_set(attributes,'$.source_kind','message')")
    assert record_self_caught_correction(db, event)


def test_sent_back_on_another_node_of_the_commitment_suppresses(db, monkeypatch):
    from core.self_caught_corrections import record_self_caught_correction
    from core.sent_back_corrections import record_rejection_correction

    source, event = _seed_event(db)
    other = _task(db, source.plan_id, "Another node")
    db.execute("UPDATE plan_nodes SET commitment_id=? WHERE id=?", (source.commitment_id, other.id))
    monkeypatch.setattr(service, "_now", lambda: "2000-01-01T00:00:00+00:00")
    rejection = service._event(db, other.id, "review_rejected", actor_id="owner", provenance="test")
    assert record_rejection_correction(db, rejection)
    assert not record_self_caught_correction(db, event)


@pytest.mark.parametrize("rejection_first", [True, False])
def test_backfill_matches_live_event_order(db, rejection_first, monkeypatch):
    from core.self_caught_corrections import record_self_caught_correction
    from core.sent_back_corrections import record_rejection_correction
    from infra.migration_150_to_151_self_caught_corrections import migrate_150_to_151

    early, late = "2000-01-01T00:00:00+00:00", "2000-01-02T00:00:00+00:00"
    monkeypatch.setattr(service, "_now", lambda: late if rejection_first else early)
    source, correction = _seed_event(db)
    monkeypatch.setattr(service, "_now", lambda: early if rejection_first else late)
    rejection = service._event(db, source.id, "review_rejected", actor_id="owner", provenance="test")
    if rejection_first:
        record_rejection_correction(db, rejection)
        record_self_caught_correction(db, correction)
    else:
        record_self_caught_correction(db, correction)
        record_rejection_correction(db, rejection)
    live = {row[0] for row in db.execute("SELECT subject_key FROM outcome_events")}
    assert len(live) == (1 if rejection_first else 2)
    db.execute("DELETE FROM outcome_events WHERE subject_key LIKE 'plan-correction-created:%'")
    migrate_150_to_151(db)
    assert {row[0] for row in db.execute("SELECT subject_key FROM outcome_events")} == live


def test_sent_back_at_same_timestamp_suppresses(db, monkeypatch):
    from core.self_caught_corrections import record_self_caught_correction
    from core.sent_back_corrections import record_rejection_correction

    monkeypatch.setattr(service, "_now", lambda: "2000-01-01T00:00:00+00:00")
    source, correction = _seed_event(db)
    rejection = service._event(db, source.id, "review_rejected", actor_id="owner", provenance="test")
    assert record_rejection_correction(db, rejection)
    assert not record_self_caught_correction(db, correction)


def test_self_caught_is_attributed_to_work_but_not_a_person_correction(db):
    from core.correction_attribution import attributed_facts
    from core.self_caught_corrections import record_self_caught_correction
    from jobs.learning_report import PERSON_CORRECTION_SOURCES

    source, event = _seed_event(db)
    assert record_self_caught_correction(db, event)
    assert "self_caught" not in PERSON_CORRECTION_SOURCES
    # KPI design 4.2 includes self-caught corrections in workflow attribution.
    (fact,) = attributed_facts(db)
    assert (fact.source_kind, fact.source_id, fact.node_id, fact.is_correction) == (
        "self_caught", event, source.id, True,
    )


def test_startup_migration_backfills_and_runs_only_once(db):
    from infra.database_migrations import migrate_to_current
    from infra.schema import SCHEMA_VERSION

    _, event = _seed_event(db)
    db.execute("PRAGMA user_version=150")
    db.commit()
    assert migrate_to_current(db, SCHEMA_VERSION).migrated
    assert db.execute("SELECT subject_key FROM outcome_events").fetchone()[0] == (
        f"plan-correction-created:{event}"
    )
    assert not migrate_to_current(db, SCHEMA_VERSION).migrated
    assert db.execute("SELECT COUNT(*) FROM outcome_events").fetchone()[0] == 1


def test_migration_failure_rolls_back_through_real_runner(db, monkeypatch):
    from infra.database_migrations import MigrationError, migrate_to_current
    from infra.schema import SCHEMA_VERSION

    _seed_event(db)
    db.execute("PRAGMA user_version=150")
    db.commit()
    before = list(db.iterdump())
    from core import self_caught_corrections
    original = self_caught_corrections.record_self_caught_correction

    def fail_after_write(connection, event_id):
        original(connection, event_id)
        raise sqlite3.OperationalError("injected failure")

    monkeypatch.setattr(self_caught_corrections, "record_self_caught_correction", fail_after_write)
    with pytest.raises(MigrationError, match="injected failure"):
        migrate_to_current(db, SCHEMA_VERSION)
    assert list(db.iterdump()) == before
    assert db.execute("PRAGMA user_version").fetchone()[0] == 150
