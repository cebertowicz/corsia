from __future__ import annotations

import sqlite3

import pytest

from automation.script_readings import build_report
from automation.script_sandbox import ScriptRun
from automation.schedules import Schedule
from infra.measure_registry import REGISTRY, admit, sync
from infra.outcome_events_schema import OUTCOME_EVENTS_SCHEMA_SQL


MEASURES = """
schema: corsia.measures/1
namespace: mail
# agent-level declaration: no workflow
events:
  mail.triaged:
    attributes:
      message_ref: id
      action: {enum: [replied, archived]}
    dedupe: [message_ref, run_id]
kpis:
  replied_share:
    title: Triaged mail answered
    unit: percent
    better: higher
    formula: share
    num: {event: mail.triaged, attr: action, values: [replied]}
    min_n: 20
"""


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    return connection


def test_sync_then_admit_declared_event() -> None:
    connection = db()
    result = sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=MEASURES)
    assert result["items"][0]["status"] == "added"
    admitted = admit(connection, owner_thread_id="agent-a", event_name="mail.triaged", attributes={"message_ref": "m-1", "action": "replied"}, project_id="project", run_id="run-1")
    assert admitted.refusal is None
    assert admitted.result and admitted.result.inserted
    row = connection.execute("SELECT source,provenance,attribution,agent_thread_id,project_id FROM outcome_events").fetchone()
    assert row == ("agent", "agent_recorded", "caller", "agent-a", "project")


def test_shared_admission_dedupes_identity_and_allows_otlp_source() -> None:
    connection = db()
    sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=MEASURES)
    first = admit(connection, owner_thread_id="agent-a", event_name="mail.triaged", attributes={"message_ref": "m-1", "action": "replied"}, run_id="run-1")
    duplicate = admit(connection, owner_thread_id="agent-a", event_name="mail.triaged", attributes={"message_ref": "m-1", "action": "replied"}, run_id="run-1")
    rerun = admit(connection, owner_thread_id="agent-a", event_name="mail.triaged", attributes={"message_ref": "m-1", "action": "replied"}, source="otlp", run_id="run-2")
    assert first.result and first.result.inserted
    assert duplicate.result and not duplicate.result.inserted
    assert rerun.result and rerun.result.inserted
    assert connection.execute("SELECT n FROM outcome_event_counters WHERE counter='duplicate_ignored'").fetchone() == (1,)


def test_undeclared_event_is_refused_and_counted() -> None:
    connection = db()
    sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=MEASURES)
    admitted = admit(connection, owner_thread_id="agent-a", event_name="mail.unknown", attributes={})
    assert admitted.result is None
    assert "not declared" in admitted.refusal
    assert connection.execute("SELECT n FROM outcome_event_counters WHERE counter='name_undeclared'").fetchone() == (1,)


def test_script_measure_line_keeps_runner_identity() -> None:
    schedule = Schedule(id="schedule", thread_id="agent-a", scope_id="project", kind="script", spec="300", next_run_at="2026-01-01T00:00:00+00:00", max_runs=1, state="active", last_run_at=None, runs=0, created_by_actor="agent-a", created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00", payload={"series": []})
    report = build_report(schedule, ScriptRun(0, b'{"measure":{"event.name":"mail.triaged","attributes":{"run_id":"forged"}}}\n', b"", False), run_id="runner-uuid")
    assert report.run_id == "runner-uuid"
    assert report.measures == [{"event.name": "mail.triaged", "attributes": {"run_id": "forged"}}]


def test_unowned_workflow_is_identity_mismatch() -> None:
    connection = db()
    sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=MEASURES)
    refused = admit(connection, owner_thread_id="agent-a", event_name="mail.triaged", attributes={"message_ref": "m-1"}, workflow_ref="another-workflow")
    assert refused.result is None
    assert "owned by another agent" in refused.refusal
    assert connection.execute("SELECT n FROM outcome_event_counters WHERE counter='identity_mismatch'").fetchone() == (1,)


def test_workflow_without_skill_owner_is_refused(tmp_path) -> None:
    connection = db()
    path = tmp_path / "measures.yaml"
    with pytest.raises(ValueError, match="workflow inbox-triage has no owner"):
        sync(connection, owner_thread_id="agent-a", path=str(path), text=MEASURES.replace("# agent-level declaration: no workflow", "workflow: inbox-triage"))


def test_version_attr_needs_workflow() -> None:
    connection = db()
    with pytest.raises(ValueError, match="version_attr is allowed only"):
        sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=MEASURES.replace("workflow: inbox-triage\n", "").replace("events:", "version_attr: revision\nevents:"))
