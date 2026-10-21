"""Declared-event intake reaches the live evaluator and platform delivery path."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from automation import script_runner, script_schedules
from automation.script_readings import RunReport
from events import intake
from events.intake import admit_event
from infra.database import get_db
from infra.measure_registry import REGISTRY, sync
from tests.event_routing_helpers import mk_session

OWNER, OTHER = "intake-owner", "intake-other"


def _declarations() -> str:
    return """schema: corsia.measures/1
namespace: qa
events:
  qa.route:
    attributes: {code: id}
    dedupe: [code]
    use: route
  qa.record:
    attributes: {code: id}
    dedupe: [code]
  qa.number:
    attributes: {cost: num}
    dedupe: [cost]
    use: route
  qa.both:
    attributes: {code: id}
    dedupe: [code, run_id]
    use: both
"""


@pytest.fixture
def intake_world(isolated_db, tmp_path: Path):
    REGISTRY.reset()
    mk_session(OWNER, project_id="project-a")
    mk_session(OTHER, project_id="project-a")
    db = get_db()
    folder = tmp_path / ".threads" / OWNER
    folder.mkdir(parents=True)
    path = folder / "measures.yaml"
    text = _declarations()
    path.write_text(text)
    sync(db, owner_thread_id=OWNER, path=str(path), text=text)
    db.commit()
    return db


def _admit(**kwargs):
    values = {
        "event_id": "event-1", "origin": "emitter", "producer": OWNER,
        "event_type": "qa.route", "facts": {"code": "ok"},
    }
    values.update(kwargs)
    return asyncio.run(admit_event(**values))


def _every_rule(db, *, rule_id: str, destination: str, mode: str = "wake") -> None:
    db.execute(
        'INSERT INTO event_rules '
        '(id,created_at,updated_at,scope,connector,event_type,"match","on",action,"to",mode,precedence) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            rule_id, "t", "t", "project:project-a", "qa", "route",
            json.dumps([{"field": "code", "op": "eq", "value": "ok"}]),
            "every", "deliver", f"thread:{destination}", mode, 10,
        ),
    )
    db.commit()


@pytest.mark.parametrize(("kwargs", "code"), [
    ({"facts": []}, "facts_not_object"),
    ({"facts": {"code": "x" * (65 * 1024)}}, "too_large"),
    ({"event_type": "qa.missing"}, "undeclared_type"),
    ({"facts": {"outside": "x"}}, "undeclared_fact"),
    ({"facts": {"code": 3}}, "bad_fact_kind"),
    ({"producer": OTHER}, "not_your_namespace"),
])
def test_declared_intake_refusal_codes(intake_world, kwargs, code):
    result = _admit(**kwargs)
    assert result.accepted is False and result.refusal and result.refusal.code == code


def test_record_only_type_is_refused_with_safe_fix(intake_world):
    result = _admit(event_type="qa.record")
    assert result.refusal and result.refusal.code == "undeclared_type"
    assert "use: record" in result.refusal.text and "qa.record" in result.refusal.text


@pytest.mark.parametrize("origin", ["connector", "emitter", "agent"])
def test_worker_events_are_reserved_outside_platform(intake_world, origin):
    result = asyncio.run(admit_event(
        event_id=f"worker-{origin}", origin=origin, producer=OWNER,
        event_type="worker.result", facts={},
    ))
    assert result.refusal and result.refusal.code == "reserved_type"


def test_accepted_retry_returns_duplicate_without_replaying_delivery(intake_world, monkeypatch):
    _every_rule(intake_world, rule_id="intake-every", destination=OTHER)
    wakes = []

    def record_wake(*args, **kwargs):
        wakes.append((args, kwargs))

    monkeypatch.setattr("events.platform.enqueue_event_wake", record_wake)
    accepted = _admit()
    duplicate = _admit()

    assert accepted.accepted and not accepted.duplicate
    assert duplicate.accepted and duplicate.duplicate
    assert accepted.rules_matched == duplicate.rules_matched == 1
    assert len(wakes) == 1


def test_declared_event_uses_the_live_every_evaluator_and_platform_path(intake_world, monkeypatch):
    _every_rule(intake_world, rule_id="intake-every", destination=OTHER)
    wakes = []

    def record_wake(*args, **kwargs):
        wakes.append((args, kwargs))

    monkeypatch.setattr("events.platform.enqueue_event_wake", record_wake)
    result = _admit(event_id="every-event")
    decisions = intake_world.execute(
        "SELECT rule_id,thread_id,outcome FROM event_route_decisions WHERE event_id=?",
        ("every-event",),
    ).fetchall()

    assert result.accepted and result.rules_matched == 1
    assert len(wakes) == 1
    assert {(row[0], row[1], row[2]) for row in decisions} == {
        ("builtin:0", OWNER, "digested"),
        ("intake-every", OTHER, "woke"),
    }


def test_refused_id_can_retry_after_fix_without_persisting_facts(intake_world):
    refused = _admit(facts={"outside": "secret"})
    accepted = _admit(facts={"code": "fixed"})
    stored = intake_world.execute(
        "SELECT event_id,outcome,rules_matched FROM event_intake_records WHERE event_id='event-1'",
    ).fetchone()

    assert refused.refusal and accepted.accepted and not accepted.duplicate
    assert tuple(stored) == ("event-1", "accepted", 0)


def test_connector_manifest_accepts_nullable_pr_number_and_refuses_unknown_fact(intake_world):
    accepted = asyncio.run(admit_event(
        event_id="github-ok", origin="connector", producer="github",
        event_type="github.head.checks_completed",
        facts={"repo": "r", "head_sha": "s", "head_branch": "main", "pr_number": None, "conclusion": "success"},
    ))
    refused = asyncio.run(admit_event(
        event_id="github-bad", origin="connector", producer="github",
        event_type="github.head.checks_completed",
        facts={"repo": "r", "head_sha": "s", "outside": "x"},
    ))
    assert accepted.accepted and refused.refusal and refused.refusal.code == "undeclared_fact"


def test_missing_resolved_envelope_is_refused_without_an_assertion(intake_world, monkeypatch):
    monkeypatch.setattr(
        intake, "_declared_event",
        lambda *_args: intake._ResolvedEvent(None, None, None, None),
    )

    result = _admit()

    assert result.refusal and result.refusal.code == "unresolved_event"
    assert result.refusal.text == "event could not be resolved; this is a platform fault"


def _stored(db, name: str) -> list[tuple]:
    return [tuple(row) for row in db.execute(
        "SELECT agent_thread_id, project_id, json_extract(attributes,'$.code'), json_extract(attributes,'$.run_id') "
        "FROM outcome_events WHERE event_name=?", (name,),
    ).fetchall()]


def test_a_both_event_is_stored_once_and_routed(intake_world):
    result = _admit(event_type="qa.both", event_id="both-1")
    retry = _admit(event_type="qa.both", event_id="both-1")
    routed = intake_world.execute(
        "SELECT rule_id, thread_id FROM event_route_decisions WHERE event_id='both-1'",
    ).fetchall()

    assert result.accepted and not result.duplicate and retry.duplicate
    assert _stored(intake_world, "qa.both") == [(OWNER, "project-a", "ok", "both-1")]
    assert [tuple(row) for row in routed] == [("builtin:0", OWNER)]
    assert _admit(event_id="route-1").accepted and _stored(intake_world, "qa.route") == []


def test_a_both_event_the_store_refuses_is_not_routed(intake_world):
    # Valid for routing (no undeclared fact) but the store's dedupe needs code.
    result = _admit(event_type="qa.both", event_id="both-refused", facts={})
    routed = intake_world.execute("SELECT 1 FROM event_route_decisions WHERE event_id='both-refused'").fetchall()
    accepted = intake_world.execute("SELECT 1 FROM event_intake_records WHERE event_id='both-refused'").fetchall()

    assert result.refusal and result.refusal.code == "record_refused" and "dedupe needs" in result.refusal.text
    assert routed == [] and accepted == []


def test_script_event_lines_route_and_a_refusal_becomes_an_owner_error(intake_world, tmp_path):
    path = tmp_path / "events.py"
    path.write_text("print('event')")
    schedule = script_schedules.create_script(
        OWNER, "project", script_path=path, script_sha256=script_schedules.file_sha256(path),
        cadence_seconds=600, series=[], timeout_seconds=30,
        now="2026-01-01T00:00:00+00:00", db=intake_world,
    )
    accepted = asyncio.run(script_runner._admit_events(
        schedule, RunReport("ok", [], [], events=[{"type": "qa.route", "facts": {"code": "ok"}}], run_id="run-1"),
    ))
    refused = asyncio.run(script_runner._admit_events(
        schedule, RunReport("ok", [], [], events=[{"type": "qa.route", "facts": {"outside": "x"}}], run_id="run-2"),
    ))

    both = asyncio.run(script_runner._admit_events(
        schedule, RunReport("ok", [], [], events=[{"type": "qa.both", "facts": {"code": "ok"}}], run_id="run-3"),
    ))

    assert accepted.status == "ok" and both.status == "ok"
    assert refused.status == "error" and refused.error == "event line refused: undeclared_fact"
    assert refused.signals[-1].kind == "error" and "undeclared_fact" in refused.signals[-1].summary
    # The run's stamp, the same one a measure line from this run gets, so the two dedupe together.
    assert _stored(intake_world, "qa.both") == [(OWNER, "project", "ok", f"script:{schedule.id}:run-3")]
