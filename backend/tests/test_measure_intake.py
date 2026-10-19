"""Declared measure intake: script lines and OTLP/JSON records through one admission function."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from automation import script_schedules as store
from automation.schedules import get as get_schedule
from automation.script_readings import build_report, emit_report
from automation.script_sandbox import ScriptRun
from infra.measure_admission import admit
from infra.measure_otlp import admit_otlp_record
from infra.measure_registry import REGISTRY, sync
from infra.measure_status import status
from infra.schema import SCHEMA_SQL
from tests.plan_writer_authorizer import raw_plan_seed

QG, OTHER, WORKER = "agent-qg", "agent-other", "worker-of-qg"
T0 = "2026-01-01T12:00:00+00:00"
FIXTURE = Path(__file__).parent / "fixtures" / "measures" / "qg_measures.yaml"
GOOD = {"pr": "907", "head_sha": "abfa9122", "model": "m", "reviewer_version": "r1", "verdict": "block", "blocks": 2, "flags": 1}


def world(tmp_path: Path) -> sqlite3.Connection:
    """A disposable database where agent-qg owns a copy of QG's file and its workflow."""
    REGISTRY.refusals.clear()
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    for thread, spawned_by in ((QG, None), (OTHER, None), (WORKER, QG)):
        db.execute("INSERT INTO threads (id,user_id,created_at,updated_at,spawned_by) VALUES (?,?,?,?,?)", (thread, "user", T0, T0, spawned_by))
    folder = tmp_path / ".threads" / QG / "skills" / "llm-pr-review"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: llm-pr-review\ncorsia:\n  workflow:\n    owner: {QG}\n---\n")
    (folder / "measures.yaml").write_text(FIXTURE.read_text())
    sync(db, owner_thread_id=QG, path=str(folder / "measures.yaml"), text=FIXTURE.read_text())
    return db


def node(db: sqlite3.Connection, node_id: str, owner: str, *, workflow_ref: str | None = None, workflow_version: str | None = None) -> None:
    plan = f"plan-{owner}"
    with raw_plan_seed(db):
        db.execute("INSERT OR IGNORE INTO plans (id,owner_kind,owner_id,created_at,updated_at) VALUES (?,?,?,?,?)", (plan, "thread", owner, T0, T0))
        db.execute(
            "INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,commitment_id,created_at,updated_at,workflow_ref,workflow_version) VALUES (?,?,0,'task','t','c',?,?,?,?,?)",
            (node_id, plan, node_id, T0, T0, workflow_ref, workflow_version),
        )


def schedule(db: sqlite3.Connection, tmp_path: Path, *, workflow: str | None = "llm-pr-review"):
    path = tmp_path / "measure.py"
    path.write_text("print('x')")
    return store.create_script(QG, "project-1", script_path=path, script_sha256=store.file_sha256(path), cadence_seconds=600, series=[], timeout_seconds=30, now=T0, workflow=workflow, db=db)


def run_script(db: sqlite3.Connection, sched, lines: list[dict], run_id: str) -> None:
    """One script run: the runner stamps its own run id, then settles the rows."""
    stdout = "".join(json.dumps({"measure": line}) + "\n" for line in lines).encode()
    emit_report(db, get_schedule(sched.id, db=db), build_report(sched, ScriptRun(0, stdout, b"", False), run_id=run_id), now=T0)


def line(event: str, attributes: dict) -> dict:
    return {"event.name": event, "attributes": attributes}


def rows(db: sqlite3.Connection, event: str | None = None) -> list[tuple]:
    where, args = ("WHERE event_name=?", (event,)) if event else ("", ())
    return db.execute(f"SELECT event_name,source,provenance,attribution,agent_thread_id,project_id,attributes FROM outcome_events {where} ORDER BY id", args).fetchall()


def counter(db: sqlite3.Connection, name: str) -> int:
    return db.execute("SELECT coalesce(sum(n),0) FROM outcome_event_counters WHERE counter=?", (name,)).fetchone()[0]


def test_script_good_and_maybe_lines_land_and_status_shows_the_drop(tmp_path: Path) -> None:
    db = world(tmp_path)
    sched = schedule(db, tmp_path)
    run_script(db, sched, [line("qa.review.completed", GOOD), line("qa.review.completed", {**GOOD, "pr": "908", "head_sha": "feed", "verdict": "maybe"})], "uuid-1")
    landed = rows(db)
    assert len(landed) == 2
    assert all(row[1:6] == ("agent", "agent_recorded", "caller", QG, "project-1") for row in landed)
    stored = json.loads(landed[0][6])
    assert stored["run_id"] == f"script:{sched.id}:uuid-1" and stored["workflow_ref"] == "llm-pr-review"
    shown = status(db, QG)["events"]["qa.review.completed"]
    assert shown["today"] == {"rows": 2, "counters": {"attribute_dropped": 1}}
    assert shown["last_7_days"] == shown["today"]
    assert status(db, QG)["recent_refusals"] == [
        'qa.review.completed: attribute "verdict" value "maybe" is not one of pass, block, stop, error; the attribute was dropped. '
        'Send one of those values, or add "maybe" to the enum and run measure_sync.'
    ]
    assert "lost when CORSIA restarts" in status(db, QG)["refusals_note"]


def test_same_line_twice_in_one_run_adds_duplicate_ignored(tmp_path: Path) -> None:
    db = world(tmp_path)
    run_script(db, schedule(db, tmp_path), [line("qa.review.completed", GOOD)] * 2, "uuid-1")
    assert len(rows(db)) == 1 and counter(db, "duplicate_ignored") == 1
    assert status(db, QG)["events"]["qa.review.completed"]["today"]["counters"] == {"duplicate_ignored": 1}


def test_rerun_inserts_run_keyed_events_again_and_identity_keyed_events_once(tmp_path: Path) -> None:
    db = world(tmp_path)
    sched = schedule(db, tmp_path)
    lines = [line("qa.review.completed", GOOD), line("qa.review.skipped", {"pr": "907", "head_sha": "abfa9122", "reason": "draft"})]
    run_script(db, sched, lines, "uuid-1")
    run_script(db, sched, lines, "uuid-2")
    assert len(rows(db, "qa.review.completed")) == 2  # recipe includes run_id: a new runner-stamped run_id inserts again
    assert len(rows(db, "qa.review.skipped")) == 1  # identity-keyed (pr, head_sha, reason): printed on both runs, lands once
    assert counter(db, "duplicate_ignored") == 1


def test_script_cannot_forge_run_id_or_workflow_and_ignores_corsia_keys(tmp_path: Path) -> None:
    db = world(tmp_path)
    sched = schedule(db, tmp_path)
    run_script(db, sched, [line("qa.review.completed", {**GOOD, "run_id": "forged", "workflow_ref": "elsewhere", "corsia.run_id": "forged"})], "uuid-1")
    stored = json.loads(rows(db)[0][6])
    assert stored["run_id"] == f"script:{sched.id}:uuid-1" and stored["workflow_ref"] == "llm-pr-review"
    assert counter(db, "attribute_dropped") == 2  # run_id and workflow_ref, the server's own stamps


def test_script_workflow_the_agent_does_not_own_stamps_nothing(tmp_path: Path) -> None:
    db = world(tmp_path)
    run_script(db, schedule(db, tmp_path, workflow="someone-elses"), [line("qa.review.completed", GOOD)], "uuid-1")
    assert "workflow_ref" not in json.loads(rows(db)[0][6])


def test_undeclared_name_inserts_nothing_and_is_counted(tmp_path: Path) -> None:
    db = world(tmp_path)
    run_script(db, schedule(db, tmp_path), [line("qa.reveiw.completed", GOOD)], "uuid-1")
    assert rows(db) == [] and counter(db, "name_undeclared") == 1
    assert status(db, QG)["recent_refusals"][0].startswith('"qa.reveiw.completed" is not declared in your measures.yaml (declared: qa.audit.label, qa.escape.found, qa.review.completed')


def test_another_agents_namespace_is_refused_and_counted(tmp_path: Path) -> None:
    db = world(tmp_path)
    refused = admit(db, owner_thread_id=OTHER, event_name="qa.review.completed", attributes=GOOD, run_id="r")
    assert refused.result is None and refused.refusal.startswith('Namespace "qa" belongs to another agent (thread agent-qg); you own no namespace yet')
    assert counter(db, "namespace_not_owned") == 1 and rows(db) == []


def fixture_record(attributes: dict, **top) -> dict:
    items = [{"key": key, "value": {"stringValue": value} if isinstance(value, str) else {"intValue": str(value)}} for key, value in attributes.items()]
    return {"attributes": items, **top}


def test_otlp_fixture_gives_the_same_row_as_the_script_path_apart_from_source(tmp_path: Path) -> None:
    script_db, otlp_db = world(tmp_path), world(tmp_path / "second")
    admit(script_db, owner_thread_id=QG, event_name="qa.review.completed", attributes=GOOD, source="agent", project_id="project-1", run_id="run-1", ts="2026-01-01T12:00:00+00:00")
    record = fixture_record({"event.name": "qa.review.completed", "corsia.run_id": "run-1", **GOOD}, timeUnixNano="1767268800000000000")
    admitted = admit_otlp_record(otlp_db, record, agent_thread_id=QG, project_id="project-1")
    assert admitted.refusal is None
    first, second = rows(script_db)[0], rows(otlp_db)[0]
    assert (first[1], second[1]) == ("agent", "otlp")
    assert first[:1] + first[2:] == second[:1] + second[2:]
    ts = lambda connection: connection.execute("SELECT ts, dedupe_key FROM outcome_events").fetchone()  # noqa: E731
    assert ts(script_db) == ts(otlp_db)


def test_otlp_run_id_on_another_agents_plan_is_identity_mismatch(tmp_path: Path) -> None:
    db = world(tmp_path)
    node(db, "node-other", OTHER)
    record = fixture_record({"event.name": "qa.review.completed", "corsia.run_id": "node-other", **GOOD})
    refused = admit_otlp_record(db, record, agent_thread_id=QG, project_id=None)
    assert refused.result is None
    assert refused.refusal == "corsia.run_id names plan node node-other on another agent's plan; the record was dropped. Use a node from your own plan or a worker you spawned, or leave run_id out."
    assert rows(db) == [] and counter(db, "identity_mismatch") == 1
    assert status(db, QG)["events"]["qa.review.completed"]["today"]["counters"] == {"identity_mismatch": 1}


def test_otlp_run_id_on_own_or_spawned_workers_plan_takes_workflow_from_the_node(tmp_path: Path) -> None:
    db = world(tmp_path)
    node(db, "node-mine", QG, workflow_ref="llm-pr-review", workflow_version="v3")
    node(db, "node-worker", WORKER)
    mine = admit_otlp_record(db, fixture_record({"event.name": "qa.review.completed", "corsia.run_id": "node-mine", "corsia.workflow.ref": "someone-elses", **GOOD}), agent_thread_id=QG, project_id=None)
    worker = admit_otlp_record(db, fixture_record({"event.name": "qa.review.completed", "corsia.run_id": "node-worker", **{**GOOD, "pr": "1"}}), agent_thread_id=QG, project_id=None)
    assert mine.refusal is None and worker.refusal is None
    stored = next(parsed for parsed in (json.loads(row[6]) for row in rows(db)) if parsed["run_id"] == "node-mine")
    assert (stored["run_id"], stored["workflow_ref"], stored["workflow_version"]) == ("node-mine", "llm-pr-review", "v3")  # the emitter's own workflow claim is ignored


def test_otlp_workflow_ref_the_agent_does_not_own_is_identity_mismatch(tmp_path: Path) -> None:
    db = world(tmp_path)
    record = fixture_record({"event.name": "qa.review.completed", "corsia.run_id": "free-id", "corsia.workflow.ref": "pr-merge", **GOOD})
    refused = admit_otlp_record(db, record, agent_thread_id=QG, project_id=None)
    assert refused.refusal == 'corsia.workflow.ref "pr-merge" is owned by another agent; the record was dropped. Use a workflow you own (yours: llm-pr-review), or leave the ref out.'
    assert rows(db) == [] and counter(db, "identity_mismatch") == 1
    owned = fixture_record({"event.name": "qa.review.completed", "corsia.run_id": "free-id", "corsia.workflow.ref": "llm-pr-review", "corsia.workflow.version": "v9", **GOOD})
    assert admit_otlp_record(db, owned, agent_thread_id=QG, project_id=None).refusal is None
    assert json.loads(rows(db)[0][6])["workflow_version"] == "v9"


def test_otlp_claimed_agent_or_project_that_differs_from_the_key_is_refused(tmp_path: Path) -> None:
    db = world(tmp_path)
    base = {"event.name": "qa.review.completed", "corsia.run_id": "r", **GOOD}
    agent = admit_otlp_record(db, fixture_record({**base, "corsia.agent_thread_id": OTHER}), agent_thread_id=QG, project_id=None)
    assert agent.refusal == f"corsia.agent_thread_id says {OTHER}, but your key belongs to {QG}; the record was dropped. Remove corsia.agent_thread_id, or use the key minted for {OTHER}."
    project = admit_otlp_record(db, fixture_record({**base, "corsia.project_id": "elsewhere"}), agent_thread_id=QG, project_id="project-1")
    assert project.refusal.startswith("corsia.project_id says elsewhere, but your key belongs to project project-1")
    assert counter(db, "identity_mismatch") == 2 and rows(db) == []


def test_missing_dedupe_attribute_is_refused_with_the_prescribed_text(tmp_path: Path) -> None:
    db = world(tmp_path)
    refused = admit(db, owner_thread_id=QG, event_name="qa.audit.label", attributes={"label": "correct_block"}, run_id="r")
    assert refused.refusal == 'qa.audit.label: dedupe needs run_id and finding; "finding" is missing; the record was dropped. Send "finding" with the record.'
    assert counter(db, "record_refused") == 1 and rows(db) == []


@pytest.mark.parametrize("when", ["2020-01-02T00:00:00Z", "2999-01-01T00:00:00Z", "yesterday"])
def test_time_outside_the_window_is_refused(tmp_path: Path, when: str) -> None:
    db = world(tmp_path)
    refused = admit(db, owner_thread_id=QG, event_name="qa.review.completed", attributes=GOOD, run_id="r", ts=when)
    assert refused.refusal.startswith(f"qa.review.completed: time {when} is outside the window (not older than 400 days, not more than 5 minutes ahead)")
    assert counter(db, "record_refused") == 1 and rows(db) == []


def test_invalid_attribute_values_keep_the_row_and_explain_each_drop(tmp_path: Path) -> None:
    db = world(tmp_path)
    admitted = admit(db, owner_thread_id=QG, event_name="qa.review.completed", attributes={**GOOD, "model": "abfa9122 draft", "blocks": -1, "cost_usd": 1.5, "extra": "x"}, run_id="r")
    assert admitted.result and admitted.result.inserted
    assert set(admitted.result.dropped_keys) == {"model", "blocks", "extra"}
    joined = " | ".join(admitted.notes)
    assert 'attribute "model" value "abfa9122 draft" is not a valid id (1 to 200 characters from letters, digits and _ . : # / @ + -, and not a credential); the attribute was dropped. Send a plain id.' in joined
    assert 'attribute "blocks" value "-1" is not a valid count (a whole number, 0 or more)' in joined
    assert 'attribute "extra" is not declared' in joined


def test_registry_loads_from_the_database_after_a_restart(tmp_path: Path) -> None:
    db = world(tmp_path)
    REGISTRY.events.clear()
    REGISTRY.workflows.clear()
    REGISTRY.loaded = False
    admitted = admit(db, owner_thread_id=QG, event_name="qa.review.completed", attributes=GOOD, run_id="r", workflow="llm-pr-review")
    assert admitted.refusal is None and json.loads(rows(db)[0][6])["workflow_ref"] == "llm-pr-review"


def test_credential_looking_value_is_not_echoed_in_the_refusal_text(tmp_path: Path) -> None:
    db = world(tmp_path)
    secret = "sk-" + "a1B2c3D4" * 5
    admitted = admit(db, owner_thread_id=QG, event_name="qa.review.completed", attributes={**GOOD, "model": secret}, run_id="r")
    assert secret not in " ".join(admitted.notes) and "[credential]" in " ".join(admitted.notes)


def test_status_for_an_agent_with_no_intake_is_empty(tmp_path: Path) -> None:
    db = world(tmp_path)
    assert status(db, OTHER)["events"] == {} and status(db, OTHER)["recent_refusals"] == []
