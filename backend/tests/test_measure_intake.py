"""Declared measure intake: a script run's measure lines through the one admission function."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from automation import script_schedules as store
from automation.schedules import get as get_schedule
from automation.script_readings import build_report, emit_report
from automation.script_sandbox import ScriptRun
from infra.measure_admission import NOT_YOURS, UNDECLARED, admit
from infra.measure_registry import REGISTRY, WorkflowOwner, sync
from infra.measure_status import status
from infra.schema import SCHEMA_SQL

QG, OTHER = "agent-qg", "agent-other"
T0 = "2026-01-01T12:00:00+00:00"
FIXTURE = Path(__file__).parent / "fixtures" / "measures" / "qg_measures.yaml"
GOOD = {"pr": "907", "head_sha": "abfa9122", "model": "m", "reviewer_version": "r1", "verdict": "block", "blocks": 2, "flags": 1}
VERSION = "0123abcd+4567ef01"  # what the runner resolves for llm-pr-review before settling


def world(tmp_path: Path) -> sqlite3.Connection:
    """A disposable database where agent-qg owns a copy of QG's file and its workflow (a thread-scope skill)."""
    REGISTRY.reset()
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    for thread in (QG, OTHER):
        db.execute("INSERT INTO threads (id,user_id,created_at,updated_at) VALUES (?,?,?,?)", (thread, "user", T0, T0))
    folder = tmp_path / ".threads" / QG / "skills" / "llm-pr-review"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: llm-pr-review\n---\n")
    (folder / "measures.yaml").write_text(FIXTURE.read_text())
    owners = {"llm-pr-review": WorkflowOwner(folder, QG)}
    sync(db, owner_thread_id=QG, path=str(folder / "measures.yaml"), text=FIXTURE.read_text(), workflows=owners)
    return db


def schedule(db: sqlite3.Connection, tmp_path: Path, *, workflow: str | None = "llm-pr-review"):
    path = tmp_path / "measure.py"
    path.write_text("print('x')")
    return store.create_script(QG, "project-1", script_path=path, script_sha256=store.file_sha256(path), cadence_seconds=600, series=[], timeout_seconds=30, now=T0, workflow=workflow, db=db)


def stamp_for(sched) -> tuple[str, str] | None:
    """The runner's resolved stamp: llm-pr-review is agent-qg's; any other workflow is no one's here."""
    workflow = (sched.payload or {}).get("workflow")
    return (workflow, VERSION) if workflow == "llm-pr-review" and sched.thread_id == QG else None


def run_script(db: sqlite3.Connection, sched, lines: list[dict], run_id: str) -> None:
    """One script run: the runner stamps its own run id and resolved workflow, then settles the rows."""
    stdout = "".join(json.dumps({"measure": line}) + "\n" for line in lines).encode()
    report = build_report(sched, ScriptRun(0, stdout, b"", False), run_id=run_id)
    emit_report(db, get_schedule(sched.id, db=db), dataclasses.replace(report, workflow_stamp=stamp_for(sched)), now=T0)


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
    assert stored["workflow_version"] == VERSION
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


def test_script_cannot_forge_run_id_or_workflow(tmp_path: Path) -> None:
    db = world(tmp_path)
    sched = schedule(db, tmp_path)
    forged = {**GOOD, "run_id": "forged", "workflow_ref": "elsewhere", "workflow_version": "v0", "corsia.run_id": "forged"}
    run_script(db, sched, [line("qa.review.completed", forged)], "uuid-1")
    stored = json.loads(rows(db)[0][6])
    assert stored["run_id"] == f"script:{sched.id}:uuid-1" and stored["workflow_ref"] == "llm-pr-review" and stored["workflow_version"] == VERSION
    assert counter(db, "attribute_dropped") == 4  # the server's three stamps, and corsia.run_id (undeclared)


def test_script_workflow_the_agent_does_not_own_stamps_nothing(tmp_path: Path) -> None:
    db = world(tmp_path)
    run_script(db, schedule(db, tmp_path, workflow="someone-elses"), [line("qa.review.completed", GOOD)], "uuid-1")
    stored = json.loads(rows(db)[0][6])
    assert "workflow_ref" not in stored and "workflow_version" not in stored
    assert counter(db, "unversioned") == 1


def test_undeclared_name_inserts_nothing_and_is_counted(tmp_path: Path) -> None:
    db = world(tmp_path)
    run_script(db, schedule(db, tmp_path), [line("qa.reveiw.completed", GOOD)], "uuid-1")
    assert rows(db) == [] and counter(db, "name_undeclared") == 1
    assert [tuple(row) for row in db.execute("SELECT DISTINCT event_name FROM outcome_event_counters")] == [(UNDECLARED,)]
    assert status(db, QG)["recent_refusals"][0].startswith('"qa.reveiw.completed" is not declared in your measures.yaml (declared: qa.audit.label, qa.escape.found, qa.review.completed')


def test_printed_names_never_become_counter_rows(tmp_path: Path) -> None:
    """Counter rows stay within (declared events + fixed labels) x counters, whatever a script prints."""
    db = world(tmp_path)
    printed = [line(f"qa.made.up{n}", GOOD) for n in range(30)]
    printed += [line(f"zz.foreign{n}", GOOD) for n in range(30)]
    printed += [{"event.name": f"qa.bad{n}", "attributes": [n]} for n in range(30)]  # malformed: refused before admission
    printed += [{"event.name": "qa.review.completed", "attributes": [1]}]  # malformed, but a declared name keeps its label
    run_script(db, schedule(db, tmp_path), printed, "uuid-1")
    labels = {row[0] for row in db.execute("SELECT event_name FROM outcome_event_counters")}
    assert labels == {UNDECLARED, "(measure line)", "qa.review.completed"}
    assert counter(db, "name_undeclared") == 60 and counter(db, "record_refused") == 31


def test_another_agents_namespace_is_refused_and_counted(tmp_path: Path) -> None:
    db = world(tmp_path)
    refused = admit(db, owner_thread_id=OTHER, event_name="qa.review.completed", attributes=GOOD, run_id="r")
    assert refused.result is None and refused.refusal.startswith('Namespace "qa" belongs to another agent (thread agent-qg); you own no namespace yet')
    assert counter(db, "namespace_not_owned") == 1 and rows(db) == []
    assert [tuple(row) for row in db.execute("SELECT event_name, agent_thread_id FROM outcome_event_counters")] == [(NOT_YOURS, OTHER)]


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
    REGISTRY.reset()
    admitted = admit(db, owner_thread_id=QG, event_name="qa.review.completed", attributes=GOOD, run_id="r", workflow_ref="llm-pr-review", workflow_version=VERSION)
    assert admitted.refusal is None and json.loads(rows(db)[0][6])["workflow_ref"] == "llm-pr-review"


def test_credential_looking_value_is_not_echoed_in_the_refusal_text(tmp_path: Path) -> None:
    db = world(tmp_path)
    secret = "sk-" + "a1B2c3D4" * 5
    admitted = admit(db, owner_thread_id=QG, event_name="qa.review.completed", attributes={**GOOD, "model": secret}, run_id="r")
    assert secret not in " ".join(admitted.notes) and "[credential]" in " ".join(admitted.notes)


def test_status_for_an_agent_with_no_intake_is_empty(tmp_path: Path) -> None:
    db = world(tmp_path)
    assert status(db, OTHER)["events"] == {} and status(db, OTHER)["recent_refusals"] == []
