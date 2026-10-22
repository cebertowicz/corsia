"""Authored rules reach declared events: create checks the declaration, emit fires the rule."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from infra.database import get_db
from infra.measure_registry import REGISTRY, sync
from tests.event_routing_helpers import mk_session
from tools.event_rules import EventRulesTool

EMITTER, WRITER, STRANGER = "rule-emitter", "rule-writer", "rule-stranger"

_DECLARATIONS = """schema: corsia.measures/1
namespace: qa
events:
  qa.route:
    attributes: {code: id, cost: num}
    dedupe: [code]
    use: route
  qa.record:
    attributes: {code: id}
    dedupe: [code]
"""


@pytest.fixture
def rule_world(isolated_db, tmp_path: Path):
    REGISTRY.reset()
    for thread in (EMITTER, WRITER):
        mk_session(thread, project_id="project-a")
    mk_session(STRANGER, project_id="project-b")
    folder = tmp_path / ".threads" / EMITTER
    folder.mkdir(parents=True)
    path = folder / "measures.yaml"
    path.write_text(_DECLARATIONS)
    db = get_db()
    sync(db, owner_thread_id=EMITTER, path=str(path), text=path.read_text())
    db.commit()
    db.close()


def _tool(thread: str, **kwargs) -> str:
    return asyncio.run(EventRulesTool().execute(_thread_id=thread, **kwargs))


def _create(**kwargs) -> str:
    return _tool(WRITER, operation="create", connector="qa", event_type=kwargs.pop("event_type", "route"), **kwargs)


def test_an_emitted_event_wakes_the_writer_of_a_matching_self_rule_with_its_note(rule_world, monkeypatch):
    wakes = []
    monkeypatch.setattr("events.platform.enqueue_event_wake", lambda *args, **kwargs: wakes.append((args, kwargs)))
    rule = json.loads(_create(match=[{"field": "code", "op": "eq", "value": "ok"}], note="review it now"))

    receipt = json.loads(_tool(EMITTER, operation="emit", event_type="qa.route", facts={"code": "ok"}))
    unmatched = json.loads(_tool(EMITTER, operation="emit", event_type="qa.route", facts={"code": "other"}))

    assert receipt["rules_matched"] == 1 and unmatched["rules_matched"] == 0
    assert [args[0] for args, _ in wakes] == [WRITER]
    assert wakes[0][0][3]["rule_notes"] == ["review it now"] and wakes[0][0][3]["event_id"] == receipt["event_id"]
    db = get_db()
    outcome = db.execute(
        "SELECT rule_id, thread_id, outcome FROM event_route_decisions WHERE event_id=?", (receipt["event_id"],),
    ).fetchall()
    db.close()
    assert [tuple(row) for row in outcome] == [(rule["id"], WRITER, "woke")]


def test_create_refuses_a_num_field_naming_its_kind(rule_world):
    refused = _create(match=[{"field": "cost", "op": "eq", "value": "3"}])

    assert refused.startswith("Refused:") and "cost is a num attribute" in refused


def test_create_refuses_a_rule_on_a_record_type_naming_its_use(rule_world):
    refused = _create(event_type="record")

    assert refused.startswith("Refused:") and "use: record" in refused and "record" in refused


def test_create_refuses_a_field_the_type_did_not_declare(rule_world):
    refused = _create(match=[{"field": "outside", "op": "present"}])

    assert refused.startswith("Refused:") and "has no field 'outside'" in refused


def test_a_rule_in_another_project_does_not_fire_on_the_event(rule_world, monkeypatch):
    wakes = []
    monkeypatch.setattr("events.platform.enqueue_event_wake", lambda *args, **kwargs: wakes.append(args))
    json.loads(_tool(STRANGER, operation="create", connector="qa", event_type="route", match=[]))

    receipt = json.loads(_tool(EMITTER, operation="emit", event_type="qa.route", facts={"code": "ok"}))

    assert receipt["rules_matched"] == 0 and wakes == []
